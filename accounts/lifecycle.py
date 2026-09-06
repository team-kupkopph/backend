"""US-N1 · account deletion — the §12.7 lifecycle, one writer.

D-S7-1 · **anonymise in place, never delete the row.** Eight FKs to `account` carry no
`ON DELETE` clause, three of them explicitly `PROTECT` in Django (`rescue_case.claimed_by`,
`adoption_inquiry.adopter`, `volunteer_signup.volunteer`), so a hard `DELETE` is *refused by
the database* rather than cascading. §12.7 wants the same outcome for its own reason: public
content should be reassigned to a "deleted user", not orphaned, and the welfare record —
rescues resolved, adoptions completed, shifts worked — has to survive the person leaving.

So deletion is two steps with a window between them:

    DELETE /me   →  status='deleted' + deleted_at + sessions revoked   (this module)
    +30 days     →  PII scrubbed in place + anonymized_at              (accounts/purge.py, US-N2)

`deleted_at` and `status` move together or not at all — the M5 CHECK constraint on `account`
enforces that pair in Postgres, so nothing here can half-delete an account.
"""
from django.db import transaction
from django.utils import timezone

from accounts.models import AccountStatus


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
            "detail": signup.shift.starts_at.strftime("%a, %d %b, %I:%M%p").lstrip("0"),
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

    return account
