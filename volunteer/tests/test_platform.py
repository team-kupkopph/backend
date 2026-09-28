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
def test_a_shift_can_be_flagged(client):
    s = _shift(verified_shelter())
    res = client.post("/api/v1/moderation/flags",
                      {"target_type": "shift", "target_id": str(s.pk), "reason": "Looks like a scam"},
                      content_type="application/json", **hdr(AccountFactory()))
    assert res.status_code in (200, 201)


@pytest.mark.django_db
def test_staff_can_close_a_shift_with_a_reason(client, staff):
    s = _shift(verified_shelter())
    VolunteerSignup.objects.create(shift=s, volunteer_account=AccountFactory(), status="approved")
    assert client.post(f"/admin-api/shifts/{s.pk}/close", {}, content_type="application/json",
                       **staff).json()["error"]["code"] == "reason_required"
    res = client.post(f"/admin-api/shifts/{s.pk}/close", {"reason": "Reported as a scam."},
                      content_type="application/json", **staff)
    assert res.json() == {"cancelled_signups": 1}
    s.refresh_from_db()
    assert s.status == ShiftStatus.CLOSED
    again = client.post(f"/admin-api/shifts/{s.pk}/close", {"reason": "x"},
                        content_type="application/json", **staff)
    assert again.status_code == 409


@pytest.mark.django_db
def test_closing_a_shift_needs_a_staff_token(client):
    s = _shift(verified_shelter())
    res = client.post(f"/admin-api/shifts/{s.pk}/close", {"reason": "x"},
                      content_type="application/json", **hdr(s.shelter_account))
    assert res.status_code in (401, 403)


@pytest.mark.django_db
def test_staff_cannot_close_a_shift_that_already_happened(client, staff):
    s = _shift(verified_shelter(), hours_out=-5)
    vol = AccountFactory()
    signup = VolunteerSignup.objects.create(shift=s, volunteer_account=vol, status="approved")
    res = client.post(f"/admin-api/shifts/{s.pk}/close", {"reason": "Reported as a scam."},
                      content_type="application/json", **staff)
    assert res.json()["error"]["code"] == "shift_ended"
    assert res.status_code == 409
    s.refresh_from_db(); signup.refresh_from_db()
    assert s.status != ShiftStatus.CLOSED
    assert signup.status == "approved"
    assert not Notification.objects.filter(account=vol, type="shift_cancelled_by_shelter").exists()


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


# -- K18 · per-account throttles on requesting and posting -------------------------------
# Sized against the REAL configured rate (settings.py), not a test-local override: DRF binds
# `SimpleRateThrottle.THROTTLE_RATES` once at import time, so overriding `settings.REST_
# FRAMEWORK` mid-test never reaches it (verified: it stays at the configured 30/day). Follows
# the pattern `common/tests/test_throttles_sprint7.py` already uses for the same reason.
@pytest.fixture(autouse=True)
def _clear_throttle_history_platform():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


def _limit_for(scope):
    from django.conf import settings
    rate = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"][scope]
    return int(rate.split("/")[0])


@pytest.mark.django_db
def test_requesting_is_throttled_per_account(client):
    shelter = verified_shelter()
    vol = AccountFactory()
    limit = _limit_for("signup_create")
    codes = []
    for _ in range(limit + 1):
        s = _shift(shelter)
        codes.append(client.post(f"/api/v1/shifts/{s.pk}/signups", {"waiver_accepted": True},
                                 content_type="application/json", **hdr(vol)).status_code)
    assert codes[:limit] == [201] * limit
    assert codes[limit] == 429


@pytest.mark.django_db
def test_posting_a_shift_is_throttled_per_account(client):
    shelter = verified_shelter()
    limit = _limit_for("shift_create")
    start = timezone.now() + timezone.timedelta(hours=48)
    body = {"type": "walking", "title": "Morning dog walk", "city": "Marikina",
           "starts_at": start.isoformat(), "ends_at": (start + timezone.timedelta(hours=2)).isoformat(),
           "capacity": 3}
    codes = [client.post("/api/v1/shelter/shifts", body, content_type="application/json",
                         **hdr(shelter)).status_code
            for _ in range(limit + 1)]
    assert codes[:limit] == [201] * limit
    assert codes[limit] == 429
