from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup


def _c(account):
    c = APIClient()
    c.force_authenticate(user=account)
    return c


def _shift(shelter):
    now = timezone.now()
    return VolunteerShift.objects.create(shelter_account=shelter, type="walking",
        starts_at=now + timedelta(days=2), ends_at=now + timedelta(days=2, hours=2),
        capacity=6, status=ShiftStatus.OPEN)


def _signup(shift, vol, *, status):
    return VolunteerSignup.objects.create(shift=shift, volunteer_account=vol, status=status)


@pytest.mark.django_db
def test_roster_includes_approved_completed_no_show_excludes_others():
    shelter = AccountFactory(account_type="shelter")
    shift = _shift(shelter)
    vols = [AccountFactory(account_type="personal") for _ in range(6)]
    approved = _signup(shift, vols[0], status=SignupStatus.APPROVED)
    completed = _signup(shift, vols[1], status=SignupStatus.COMPLETED)
    no_show = _signup(shift, vols[2], status=SignupStatus.NO_SHOW)
    _signup(shift, vols[3], status=SignupStatus.REQUESTED)
    _signup(shift, vols[4], status=SignupStatus.DECLINED)
    _signup(shift, vols[5], status=SignupStatus.CANCELLED)

    body = _c(shelter).get(f"/api/v1/shelter/shifts/{shift.pk}/roster").json()
    ids = {item["signup_id"] for item in body["results"]}
    assert ids == {str(approved.pk), str(completed.pk), str(no_show.pk)}


@pytest.mark.django_db
def test_roster_item_shape_and_check_in_out_null_and_iso():
    shelter = AccountFactory(account_type="shelter")
    shift = _shift(shelter)
    vol = AccountFactory(account_type="personal")
    su = _signup(shift, vol, status=SignupStatus.APPROVED)

    body = _c(shelter).get(f"/api/v1/shelter/shifts/{shift.pk}/roster").json()
    item = body["results"][0]
    assert item["signup_id"] == str(su.pk)
    assert item["volunteer"] == {"display_name": vol.display_name}
    assert item["status"] == SignupStatus.APPROVED
    assert item["check_in_at"] is None
    assert item["check_out_at"] is None

    now = timezone.now()
    su.check_in_at = now
    su.check_out_at = now + timedelta(hours=1)
    su.save(update_fields=["check_in_at", "check_out_at"])

    body = _c(shelter).get(f"/api/v1/shelter/shifts/{shift.pk}/roster").json()
    item = body["results"][0]
    assert item["check_in_at"] == su.check_in_at.isoformat()
    assert item["check_out_at"] == su.check_out_at.isoformat()


@pytest.mark.django_db
def test_roster_ordered_by_created_at_ascending():
    shelter = AccountFactory(account_type="shelter")
    shift = _shift(shelter)
    vols = [AccountFactory(account_type="personal") for _ in range(3)]
    first = _signup(shift, vols[0], status=SignupStatus.APPROVED)
    second = _signup(shift, vols[1], status=SignupStatus.COMPLETED)
    third = _signup(shift, vols[2], status=SignupStatus.NO_SHOW)

    body = _c(shelter).get(f"/api/v1/shelter/shifts/{shift.pk}/roster").json()
    ids = [item["signup_id"] for item in body["results"]]
    assert ids == [str(first.pk), str(second.pk), str(third.pk)]


@pytest.mark.django_db
def test_roster_foreign_shelter_403():
    shelter = AccountFactory(account_type="shelter")
    other = AccountFactory(account_type="shelter")
    shift = _shift(shelter)
    vol = AccountFactory(account_type="personal")
    _signup(shift, vol, status=SignupStatus.APPROVED)
    resp = _c(other).get(f"/api/v1/shelter/shifts/{shift.pk}/roster")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "not_your_shift"


@pytest.mark.django_db
def test_roster_guest_401():
    shelter = AccountFactory(account_type="shelter")
    shift = _shift(shelter)
    resp = APIClient().get(f"/api/v1/shelter/shifts/{shift.pk}/roster")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_roster_missing_shift_404():
    shelter = AccountFactory(account_type="shelter")
    import uuid
    resp = _c(shelter).get(f"/api/v1/shelter/shifts/{uuid.uuid4()}/roster")
    assert resp.status_code == 404
