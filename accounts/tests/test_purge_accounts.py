"""US-N2 · the grace window and the purge (§12.7, D-S7-1).

The second half of deletion. `DELETE /me` flips the account and revokes its sessions; this
sweep, `ACCOUNT_PURGE_GRACE_DAYS` later, makes it irreversible by **scrubbing the PII in
place** — because the row cannot be deleted (eight FKs to `account` carry no `ON DELETE`)
and, more importantly, should not be: §12.7 keeps "non-identifying records needed for
welfare integrity", which is the whole point of the exercise. A rescue that happened still
happened after its rescuer leaves.

So the sweep has two jobs that pull in opposite directions, and both are asserted here:

  * **erase the person** — name, photo, phone, email, address, credentials;
  * **keep the welfare record** — the resolved case, the completed adoption, the worked
    shift, and the moderation audit trail, all still attached to the same (now anonymous)
    row rather than orphaned or reassigned.

⚠️ The sharpest trap is `moderation_flag.reporter_account`. NULL there means **"System"** in
the admin queue (a platform-raised flag), so nulling a departed person's flags would launder
their report into one the platform raised itself and corrupt the audit trail. It stays
pointed at the anonymised row.
"""
from datetime import timedelta

import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.lifecycle import soft_delete_account
from accounts.models import Account, AccountStatus, Address
from accounts.purge import anonymize_expired_accounts


def _deleted_days_ago(days, **kw):
    """An account soft-deleted `days` ago."""
    account = AccountFactory(**kw)
    soft_delete_account(account)
    account.deleted_at = timezone.now() - timedelta(days=days)
    account.save(update_fields=["deleted_at"])
    return account


# ---------------------------------------------------------------- the window

@pytest.mark.django_db
def test_an_active_account_is_never_touched():
    account = AccountFactory(display_name="Ana Reyes")

    anonymize_expired_accounts()

    account.refresh_from_db()
    assert account.display_name == "Ana Reyes"
    assert account.anonymized_at is None


@pytest.mark.django_db
def test_an_account_still_inside_the_grace_window_is_not_purged():
    # The window is the whole promise of "you have 30 days to change your mind".
    account = _deleted_days_ago(29, display_name="Migs G.")

    anonymize_expired_accounts()

    account.refresh_from_db()
    assert account.display_name == "Migs G."
    assert account.anonymized_at is None


@pytest.mark.django_db
def test_an_account_past_the_window_is_anonymised():
    account = _deleted_days_ago(31, display_name="Migs G.")

    purged = anonymize_expired_accounts()

    account.refresh_from_db()
    assert len(purged) == 1
    assert account.display_name == "Deleted user"      # §12.7's own words
    assert account.anonymized_at is not None
    assert account.status == AccountStatus.DELETED     # still deleted, now irreversibly


# ---------------------------------------------------------------- erasing the person

@pytest.mark.django_db
def test_every_direct_identifier_is_scrubbed():
    account = _deleted_days_ago(31, display_name="Migs G.", phone="+639170001111")
    account.photo_url = "https://example.invalid/avatar.jpg"
    account.set_password("correct-horse")
    account.save()
    Address.objects.create(account=account, city="Marikina", barangay="Concepcion Uno",
                           line1="12 Real St", is_primary=True)

    anonymize_expired_accounts()

    account.refresh_from_db()
    assert account.phone is None
    assert account.photo_url == ""
    assert account.password_hash == ""                 # the account can never be signed into
    assert not Address.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_the_email_becomes_an_unroutable_sentinel_that_still_satisfies_the_unique_index():
    # `email` is NOT NULL UNIQUE, so it cannot simply be blanked — two purged accounts would
    # collide on "". A per-account address in a reserved, unroutable domain keeps the index
    # satisfiable and guarantees nothing can ever be sent to it.
    first = _deleted_days_ago(31)
    second = _deleted_days_ago(31)

    anonymize_expired_accounts()

    first.refresh_from_db(); second.refresh_from_db()
    assert first.email != second.email
    assert first.email.endswith(".invalid")            # RFC 2606 — cannot resolve
    assert "@" in first.email


@pytest.mark.django_db
def test_the_original_address_is_freed_for_a_returning_user(client):
    # Consequence of the sentinel, and the answer to "can I come back?": once anonymised,
    # the address no longer belongs to anyone, so signing up again succeeds.
    _deleted_days_ago(31, email="returning@kupkop.ph")
    anonymize_expired_accounts()

    res = client.post("/api/v1/auth/signup",
                      {"email": "returning@kupkop.ph", "password": "Str0ng!passw0rd",
                       "display_name": "Back Again", "account_type": "personal",
                       "terms_consent": True},
                      content_type="application/json")

    assert res.status_code == 201


# ---------------------------------------------------------------- keeping the record

@pytest.mark.django_db
def test_a_resolved_rescue_survives_attributed_to_a_deleted_user():
    from sagip.models import RescueCase, StrayReport
    account = _deleted_days_ago(31, display_name="Migs G.")
    report = StrayReport.objects.create(species="dog", condition="injured",
                                        geom=Point(121.05, 14.63, srid=4326))
    case = RescueCase.objects.create(report=report, claimed_by_account=account,
                                     resolved_at=timezone.now())

    anonymize_expired_accounts()

    case.refresh_from_db()
    # Still there, still attached to the same row — not orphaned, not reassigned.
    assert case.claimed_by_account_id == account.pk
    assert case.claimed_by_account.display_name == "Deleted user"


@pytest.mark.django_db
def test_a_completed_shift_survives():
    from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup
    account = _deleted_days_ago(31)
    shift = VolunteerShift.objects.create(
        shelter_account=AccountFactory(), starts_at=timezone.now() - timedelta(days=40),
        ends_at=timezone.now() - timedelta(days=40) + timedelta(hours=2), capacity=4)
    signup = VolunteerSignup.objects.create(shift=shift, volunteer_account=account,
                                            status=SignupStatus.COMPLETED)

    anonymize_expired_accounts()

    signup.refresh_from_db()
    assert signup.status == SignupStatus.COMPLETED
    assert signup.volunteer_account_id == account.pk


@pytest.mark.django_db
def test_a_departed_persons_moderation_flag_still_reads_as_a_persons():
    """The trap. NULL reporter renders as "System" in the admin queue (a platform-raised
    flag), so nulling this would launder a person's report into the platform's own and
    corrupt the audit trail. It must stay pointed at the anonymised row."""
    from moderation.models import ModerationFlag
    account = _deleted_days_ago(31)
    flag = ModerationFlag.objects.create(
        reporter_account=account, target_type="listing",
        target_id="00000000-0000-0000-0000-000000000000", reason="spam")

    anonymize_expired_accounts()

    flag.refresh_from_db()
    assert flag.reporter_account_id == account.pk      # NOT None — never "System"
    assert flag.reporter_account.display_name == "Deleted user"


@pytest.mark.django_db
def test_the_verification_decision_record_survives():
    # §12.6 keeps the decision for the audit trail; only the ID images go, and they go on
    # their own 90-day clock (US-SEC4), not this one.
    from verifications.models import VerificationRequest
    account = _deleted_days_ago(31)
    request = VerificationRequest.objects.create(account=account, type="rescuer",
                                                 status="approved")

    anonymize_expired_accounts()

    request.refresh_from_db()
    assert request.status == "approved"
    assert request.account_id == account.pk


# ---------------------------------------------------------------- sweep hygiene

@pytest.mark.django_db
def test_the_sweep_is_idempotent():
    account = _deleted_days_ago(31)
    anonymize_expired_accounts()
    account.refresh_from_db()
    stamped_at, email = account.anonymized_at, account.email

    second = anonymize_expired_accounts()

    account.refresh_from_db()
    assert second == []                                # already-stamped rows are excluded
    assert account.anonymized_at == stamped_at         # and never re-stamped
    assert account.email == email                      # nor re-sentinelled


@pytest.mark.django_db
def test_the_sweep_handles_several_accounts_and_reports_what_it_touched():
    _deleted_days_ago(31); _deleted_days_ago(45)
    _deleted_days_ago(2)                               # still in grace
    AccountFactory()                                   # active

    purged = anonymize_expired_accounts()

    assert len(purged) == 2
    assert Account.objects.filter(anonymized_at__isnull=False).count() == 2


@pytest.mark.django_db
def test_the_management_command_runs_the_sweep():
    from io import StringIO

    from django.core.management import call_command
    account = _deleted_days_ago(31)

    out = StringIO()
    call_command("purge_deleted_accounts", stdout=out)

    account.refresh_from_db()
    assert account.anonymized_at is not None
    assert "1" in out.getvalue()
