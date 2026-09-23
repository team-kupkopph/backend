import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import AccountFactory
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup


def _shift(**kw):
    defaults = dict(
        shelter_account=AccountFactory(account_type="shelter"),
        type="walking",
        starts_at=timezone.now() + timezone.timedelta(days=2),
        ends_at=timezone.now() + timezone.timedelta(days=2, hours=2),
        capacity=2,
    )
    defaults.update(kw)
    return VolunteerShift.objects.create(**defaults)


@pytest.mark.django_db
def test_shift_defaults_to_open_walking():
    s = _shift()
    assert s.status == ShiftStatus.OPEN
    assert s.type == "walking"
    assert s.updated_at is not None


@pytest.mark.django_db
def test_signup_defaults_to_requested_with_consents_false():
    s = _shift()
    su = VolunteerSignup.objects.create(shift=s, volunteer_account=AccountFactory())
    assert su.status == SignupStatus.REQUESTED
    assert su.waiver_accepted is False
    assert su.contact_share_consent is False
    assert su.waiver_accepted_at is None
    assert su.waiver_version == ""
    assert su.cancelled_at is None


@pytest.mark.django_db
def test_one_live_signup_per_volunteer_per_shift():
    s = _shift()
    vol = AccountFactory()
    first = VolunteerSignup.objects.create(shift=s, volunteer_account=vol)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            VolunteerSignup.objects.create(shift=s, volunteer_account=vol)

    # D4 · a cancelled row is history, not a lock — asking again is allowed.
    first.status = SignupStatus.CANCELLED
    first.save()
    VolunteerSignup.objects.create(shift=s, volunteer_account=vol)   # allowed now


@pytest.mark.django_db
def test_a_different_volunteer_may_sign_up_for_the_same_shift():
    s = _shift()
    VolunteerSignup.objects.create(shift=s, volunteer_account=AccountFactory())
    VolunteerSignup.objects.create(shift=s, volunteer_account=AccountFactory())
    assert VolunteerSignup.objects.filter(shift=s).count() == 2


@pytest.mark.django_db
def test_ends_at_must_follow_starts_at():
    now = timezone.now()
    with pytest.raises(IntegrityError):
        _shift(starts_at=now + timezone.timedelta(hours=3),
               ends_at=now + timezone.timedelta(hours=1))


@pytest.mark.django_db
def test_capacity_must_be_at_least_one():
    with pytest.raises(IntegrityError):
        _shift(capacity=0)
