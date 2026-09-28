"""P6 · platform & staff (review §2.4, G16, G21, G22; test plan K18, K22, K23)."""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from adminapi.tests.conftest import full_signin, staffer  # noqa: F401 — `staffer` is a fixture
from notifications.models import Notification
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.tests.helpers import hdr, verified_shelter


@pytest.fixture
def staff(client, staffer):  # noqa: F811 — pytest fixture param shadowing the module import
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def _shift(shelter, hours_out=48):
    start = timezone.now() + timezone.timedelta(hours=hours_out)
    return VolunteerShift.objects.create(shelter_account=shelter, starts_at=start,
                                         ends_at=start + timezone.timedelta(hours=2), capacity=3,
                                         title="Morning dog walk", city="Marikina")


@pytest.mark.django_db
def test_suspending_a_shelter_cancels_its_future_activities_quietly(client, staff):
    shelter = verified_shelter()
    future, past = _shift(shelter), _shift(shelter, hours_out=-5)
    vol = AccountFactory()
    VolunteerSignup.objects.create(shift=future, volunteer_account=vol, status=SignupStatus.APPROVED)
    kept = VolunteerSignup.objects.create(shift=past, volunteer_account=AccountFactory(),
                                          status=SignupStatus.APPROVED)
    res = client.post(f"/admin-api/members/{shelter.account_id}/suspend",
                      {"reason": "Fake organisation."}, content_type="application/json", **staff)
    assert res.status_code == 200
    future.refresh_from_db(); kept.refresh_from_db()
    assert future.status == ShiftStatus.CLOSED
    assert kept.status == SignupStatus.APPROVED            # history is not rewritten
    n = Notification.objects.get(account=vol, type="shift_cancelled_by_shelter")
    assert n.body == "This activity is no longer running."
    assert "suspend" not in (n.title + n.body).lower()


@pytest.mark.django_db
def test_the_shelters_own_cancel_still_works_through_the_service(client):
    shelter = verified_shelter()
    s = _shift(shelter)
    VolunteerSignup.objects.create(shift=s, volunteer_account=AccountFactory(), status="requested")
    res = client.post(f"/api/v1/shelter/shifts/{s.pk}/cancel", **hdr(shelter))
    assert res.json() == {"cancelled_signups": 1}
    assert VolunteerSignup.objects.get(shift=s).cancelled_by == "shelter"


@pytest.mark.django_db
def test_a_shelter_with_booked_volunteers_is_blocked_from_deleting():
    from accounts.lifecycle import open_commitments
    shelter = verified_shelter()
    s = _shift(shelter)
    VolunteerSignup.objects.create(shift=s, volunteer_account=AccountFactory(), status="approved")
    kinds = [b["kind"] for b in open_commitments(shelter)]
    assert kinds == ["hosted_shift"]


@pytest.mark.django_db
def test_blocker_times_are_local():
    from zoneinfo import ZoneInfo

    from accounts.lifecycle import open_commitments
    shelter = verified_shelter()
    start = timezone.datetime(2031, 10, 4, 9, 0, tzinfo=ZoneInfo("Asia/Manila"))
    s = VolunteerShift.objects.create(shelter_account=shelter, starts_at=start,
                                      ends_at=start + timezone.timedelta(hours=2), capacity=2,
                                      title="Morning dog walk")
    vol = AccountFactory()
    VolunteerSignup.objects.create(shift=s, volunteer_account=vol, status="approved")
    assert "9:00AM" in open_commitments(vol)[0]["detail"]
    assert "9:00AM" in open_commitments(shelter)[0]["detail"]
