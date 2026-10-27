"""US-N1 · account deletion — the §12.7 lifecycle, one writer.

D-S7-1 · **anonymise in place, never delete the row.** Eight FKs to `account` carry no
`ON DELETE` clause, three of them explicitly `PROTECT` in Django (`rescue_case.claimed_by`,
`adoption_inquiry.adopter`, `volunteer_signup.volunteer`), so a hard `DELETE` is *refused by
the database* rather than cascading. §12.7 wants the same outcome for its own reason: public
content should be reassigned to a "deleted user", not orphaned, and the welfare record —
rescues resolved, adoptions completed, shifts worked — has to survive the person leaving.

So deletion is two steps with a window between them:

    DELETE /me   →  status='deleted' + deleted_at + sessions revoked   (this module)
    sign in      →  inside the window, the person may RESTORE (AS1, below)
    +30 days     →  PII scrubbed in place + anonymized_at              (accounts/purge.py, US-N2)

`deleted_at` and `status` move together or not at all — the M5 CHECK constraint on `account`
enforces that pair in Postgres, so nothing here can half-delete an account.
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import AccountStatus
from accounts.sessions import can_restore


def _when(dt):
    """"Sat, 04 Oct, 9:00AM" in Manila time (K23 -- this used to format the UTC value)."""
    local = timezone.localtime(dt)
    hour = local.hour % 12 or 12
    return f"{local:%a, %d %b}, {hour}:{local.minute:02d}{'AM' if local.hour < 12 else 'PM'}"


def open_commitments(account):
    """Things other people are relying on this account for, as a list of blocker dicts.

    A commitment is not unilaterally abandonable — the same posture the binding rescue claim
    takes everywhere else in Sagip. An open claim means an animal is waiting on this person;
    an approved shift means a shelter has rostered them; an active inquiry means someone is
    waiting on an answer about an animal.

    ALL blockers are collected, never the first one found: being told about one, fixing it,
    and then being told about the next is the worst version of this screen.
    """
    from listings.models import AdoptionInquiry, InquiryStatus
    from sagip.models import RescueCase
    from volunteer.models import SignupStatus, VolunteerSignup

    now = timezone.now()
    blockers = []

    # An unresolved claim: this person took custody of a live case.
    for case in (RescueCase.objects.filter(claimed_by_account=account, resolved_at__isnull=True)
                 .select_related("report")):
        blockers.append({
            "kind": "rescue_claim",
            "label": "Open rescue claim",
            "detail": f"{case.report.species} · {case.report.condition}",
            "id": str(case.pk),
        })

    # A shift still to come that they are on the roster for. Past shifts are history, not a
    # commitment — completed and no-show signups are records, and deleting must not be
    # blocked by something that already happened.
    for signup in (VolunteerSignup.objects
                   .filter(volunteer_account=account,
                           status__in=[SignupStatus.REQUESTED, SignupStatus.APPROVED],
                           shift__starts_at__gte=now)
                   .select_related("shift")):
        blockers.append({
            "kind": "volunteer_shift",
            "label": "Kawang-Gawa shift",
            "detail": _when(signup.shift.starts_at),
            "id": str(signup.pk),
        })

    # Active inquiries, from BOTH sides: the adopter is mid-conversation about an animal, and
    # a poster who vanishes strands whoever is waiting on their answer.
    inquiries = (AdoptionInquiry.objects
                 .filter(status=InquiryStatus.ACTIVE)
                 .filter(models_q_adopter_or_poster(account))
                 .select_related("listing"))
    for inquiry in inquiries:
        blockers.append({
            "kind": "adoption_inquiry",
            "label": "Adoption inquiry",
            "detail": inquiry.listing.name,
            "id": str(inquiry.pk),
        })

    # A shelter with future activity that volunteers are already rostered onto: deleting the
    # account out from under them would strand a signup nobody can now fulfil (G16).
    if account.account_type == "shelter":
        from volunteer.models import ShiftStatus, VolunteerShift

        hosted = (VolunteerShift.objects
                  .filter(shelter_account=account, starts_at__gte=now,
                          status__in=[ShiftStatus.OPEN, ShiftStatus.FULL],
                          signups__status__in=[SignupStatus.REQUESTED, SignupStatus.APPROVED])
                  .distinct())
        for shift in hosted:
            blockers.append({
                "kind": "hosted_shift", "label": "Volunteer activity",
                "detail": f"{shift.title or shift.get_type_display()} · {_when(shift.starts_at)}",
                "id": str(shift.pk),
            })

    return blockers


def models_q_adopter_or_poster(account):
    """`Q` matching an inquiry this account is either side of."""
    from django.db.models import Q
    return Q(adopter_account=account) | Q(listing__posted_by=account)


@transaction.atomic
def soft_delete_account(account):
    """Flip the account to deleted, revoke every session, and stop all push.

    Idempotent: a second call on an already-deleted account changes nothing and does NOT
    restart the grace window, because `deleted_at` is what the purge sweep counts from —
    re-stamping it would silently extend the retention of data the user asked us to erase.
    """
    if account.status == AccountStatus.DELETED:
        return account

    now = timezone.now()
    account.status = AccountStatus.DELETED
    account.deleted_at = now
    # Every issued JWT dies on its next request: AccountJWTAuthentication rejects a token
    # whose `iat` predates this. Same mechanism as logout-all and password reset.
    account.sessions_revoked_at = now
    account.save(update_fields=["status", "deleted_at", "sessions_revoked_at", "updated_at"])

    # A deleted account must stop receiving push immediately, not when the token next fails.
    from devices.models import DeviceToken
    DeviceToken.objects.filter(account=account).delete()

    # T4 · leaving must not strand the people on the other side of a pledge. Same transaction:
    # if settling fails the account is NOT deleted, rather than deleted with donors left waiting.
    _settle_commitments(account)

    return account


def _settle_commitments(account):
    """T4 · the other half of closing a need, from the other end. A leaving shelter's needs close
    and its donors are told (AS3); a leaving donor's waiting pledges are cancelled and their
    shelters told (AS4). Restoring (AS1) deliberately does NOT undo any of it: the people it
    reached were told it had ended."""
    from community import needs
    from notifications.service import notify
    from shelter.qr_rules import withdraw_for_departure

    for need, cancelled in needs.settle_departing_shelter(account):
        for pledge in cancelled:
            if pledge.pledger_account_id == account.pk:
                continue
            notify(pledge.pledger_account, "need_closed", title="Need closed",
                   body=(f"{account.display_name} left Kupkop — your pledge to “{need.title}” "
                         "was cancelled. Salamat for offering."),
                   data={"need_id": str(need.pk), "pledge_id": str(pledge.pk)})
    if account.account_type == "shelter":
        withdraw_for_departure(account)

    for pledge in needs.cancel_departing_donor_pledges(account):
        need = pledge.need
        # No name: this notification outlives the donor's erasure, and a body that says who
        # they were would be the one place it survives. The shelter knows WHICH need.
        notify(need.shelter_account, "pledge_cancelled", title="Pledge cancelled",
               body=f"A donor left Kupkop — their pledge to “{need.title}” was cancelled.",
               data={"need_id": str(need.pk), "pledge_id": str(pledge.pk)})


# ---------------------------------------------------------------- AS1 · restore

class RestoreTokenInvalid(Exception):
    """Unknown, expired, already used, or for an account that can no longer be restored. One
    exception for all four on purpose: telling them apart would tell a caller that a token
    they hold was once real."""


def _hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def purge_on(account):
    """The date the grace window closes and the PII is scrubbed, in Manila time."""
    when = account.deleted_at + timedelta(days=settings.ACCOUNT_PURGE_GRACE_DAYS)
    return timezone.localtime(when).date()


def issue_restore_token(account):
    """A single-use bearer token that lets a deleted account be restored.

    Issued only after the caller PROVED they own the account (the right password, or a
    verified Google/Apple identity), so it carries no secret an attacker could ask for. It rides
    on `verification_code` like the OTP codes do — hashed at rest, expiring, consumed once —
    but it is a long random string rather than six digits, so there is nothing to guess and no
    attempt counter to keep. A newer token replaces the older one.
    """
    from verifications.models import OtpChannel, OtpPurpose, VerificationCode

    raw = secrets.token_urlsafe(32)
    with transaction.atomic():
        VerificationCode.objects.filter(account=account, purpose=OtpPurpose.RESTORE,
                                        consumed_at__isnull=True).delete()
        VerificationCode.objects.create(
            account=account, channel=OtpChannel.EMAIL, purpose=OtpPurpose.RESTORE,
            code_hash=_hash_token(raw), max_attempts=1,
            expires_at=timezone.now() + timedelta(minutes=settings.RESTORE_TOKEN_MINUTES))
    return raw


@transaction.atomic
def restore_account(account):
    """Undo `soft_delete_account` for an account still inside its grace window.

    `sessions_revoked_at` is deliberately LEFT as it is: clearing it would re-validate every
    token issued before the deletion. The caller mints a fresh session instead.
    """
    if not can_restore(account):
        raise RestoreTokenInvalid("not restorable")
    account.status = AccountStatus.ACTIVE
    account.deleted_at = None            # the M5 CHECK requires this pair to move together
    account.save(update_fields=["status", "deleted_at", "updated_at"])
    return account


@transaction.atomic
def redeem_restore_token(raw):
    """Spend a restore token and restore its account, or raise `RestoreTokenInvalid`."""
    from verifications.models import OtpPurpose, VerificationCode

    if not isinstance(raw, str) or not raw:
        raise RestoreTokenInvalid("malformed")
    now = timezone.now()
    row = (VerificationCode.objects.select_for_update()
           .select_related("account")
           .filter(purpose=OtpPurpose.RESTORE, code_hash=_hash_token(raw),
                   consumed_at__isnull=True, expires_at__gt=now)
           .first())
    if row is None:
        raise RestoreTokenInvalid("unknown or expired")
    row.consumed_at = now
    row.save(update_fields=["consumed_at"])
    return restore_account(row.account)
