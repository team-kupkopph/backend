import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.status import StatusError, set_signup_status


def _signup(**kw):
    shift = VolunteerShift.objects.create(
        shelter_account=AccountFactory(account_type="shelter"),
        starts_at=timezone.now() + timezone.timedelta(days=2),
        ends_at=timezone.now() + timezone.timedelta(days=2, hours=2), capacity=2)
    return VolunteerSignup.objects.create(shift=shift, volunteer_account=AccountFactory(), **kw)


@pytest.mark.django_db
def test_moves_the_status():
    su = _signup()
    set_signup_status(su, SignupStatus.APPROVED)
    su.refresh_from_db()
    assert su.status == SignupStatus.APPROVED


@pytest.mark.django_db
def test_cancelling_stamps_cancelled_at():
    su = _signup()
    set_signup_status(su, SignupStatus.CANCELLED)
    su.refresh_from_db()
    assert su.cancelled_at is not None


@pytest.mark.django_db
def test_cancelled_at_survives_a_later_write():
    """The whole reason cancelled_at exists: updated_at cannot serve the 12h audit
    because any later write overwrites it."""
    su = _signup()
    set_signup_status(su, SignupStatus.CANCELLED)
    su.refresh_from_db()
    stamped = su.cancelled_at
    su.notes = "a later, unrelated write"
    su.save(update_fields=["notes", "updated_at"])
    su.refresh_from_db()
    assert su.cancelled_at == stamped
    assert su.updated_at > stamped


@pytest.mark.django_db
def test_a_later_status_change_does_not_clobber_cancelled_at():
    """set_signup_status itself, called a second time, must not touch a cancelled_at
    stamped by an earlier call — the raw-save test above only proves Django's
    update_fields mechanic, not this function's behavior."""
    su = _signup()
    set_signup_status(su, SignupStatus.CANCELLED)
    su.refresh_from_db()
    stamped = su.cancelled_at
    set_signup_status(su, SignupStatus.COMPLETED)
    su.refresh_from_db()
    assert su.cancelled_at == stamped


@pytest.mark.django_db
def test_non_cancel_transitions_leave_cancelled_at_null():
    su = _signup()
    set_signup_status(su, SignupStatus.COMPLETED)
    su.refresh_from_db()
    assert su.cancelled_at is None


@pytest.mark.django_db
def test_unknown_status_is_refused_and_does_not_mutate():
    su = _signup()
    with pytest.raises(StatusError):
        set_signup_status(su, "abducted_by_aliens")
    su.refresh_from_db()
    assert su.status == SignupStatus.REQUESTED
