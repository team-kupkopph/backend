"""P1 · trust & privacy fixes (dev/volunteer-build-review.md G3/G4/G16, test plan K1–K6)."""
from datetime import UTC

import pytest
from django.conf import settings
from django.utils import timezone as tz

from accounts.factories import AccountFactory
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.tests.helpers import hdr, verified_shelter

SHIFTS = "/api/v1/shelter/shifts"


def test_server_time_zone_is_manila():
    assert settings.TIME_ZONE == "Asia/Manila"
    assert settings.USE_TZ is True


@pytest.mark.django_db
def test_a_naive_start_time_is_refused_not_guessed(client):
    shelter = verified_shelter()
    res = client.post(SHIFTS, {"type": "walking", "starts_at": "2030-10-04T09:00",
                               "ends_at": "2030-10-04T11:00", "capacity": 2},
                      content_type="application/json", **hdr(shelter))
    assert res.status_code == 400
    err = res.json()["error"]
    assert err["field"] == "starts_at"
    assert err["code"] == "naive_datetime"
    assert VolunteerShift.objects.count() == 0


@pytest.mark.django_db
def test_an_offset_time_is_stored_as_that_instant(client):
    shelter = verified_shelter()
    res = client.post(SHIFTS, {"type": "walking", "starts_at": "2030-10-04T09:00:00+08:00",
                               "ends_at": "2030-10-04T11:00:00+08:00", "capacity": 2},
                      content_type="application/json", **hdr(shelter))
    assert res.status_code == 201
    shift = VolunteerShift.objects.get()
    assert shift.starts_at.astimezone(UTC).isoformat() == "2030-10-04T01:00:00+00:00"


@pytest.mark.django_db
def test_patch_also_refuses_naive(client):
    shelter = verified_shelter()
    start = tz.now() + tz.timedelta(days=2)
    shift = VolunteerShift.objects.create(shelter_account=shelter, starts_at=start,
                                          ends_at=start + tz.timedelta(hours=2), capacity=2)
    res = client.patch(f"{SHIFTS}/{shift.pk}", {"ends_at": "2030-10-04T11:00"},
                       content_type="application/json", **hdr(shelter))
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "naive_datetime"


def _future_shift(shelter, **kw):
    start = tz.now() + tz.timedelta(days=2)
    d = dict(shelter_account=shelter, starts_at=start,
             ends_at=start + tz.timedelta(hours=2), capacity=2)
    d.update(kw)
    return VolunteerShift.objects.create(**d)


@pytest.mark.django_db
def test_an_unverified_shelter_cannot_post(client):
    res = client.post(SHIFTS, {"type": "walking", "starts_at": "2030-10-04T09:00:00+08:00",
                               "ends_at": "2030-10-04T11:00:00+08:00", "capacity": 2},
                      content_type="application/json",
                      **hdr(AccountFactory(account_type="shelter")))
    assert res.status_code == 403
    assert "verified" in res.json()["error"]["message"]


@pytest.mark.django_db
def test_browse_hides_unverified_and_inactive_shelters(client):
    ok = _future_shift(verified_shelter())
    _future_shift(AccountFactory(account_type="shelter"))            # never verified
    _future_shift(verified_shelter(status="suspended"))
    ids = [r["shift_id"] for r in client.get("/api/v1/shifts").json()["results"]]
    assert ids == [str(ok.pk)]


@pytest.mark.django_db
def test_detail_404s_for_a_shift_that_is_not_public(client):
    hidden = _future_shift(AccountFactory(account_type="shelter"))
    assert client.get(f"/api/v1/shifts/{hidden.pk}").status_code == 404


@pytest.mark.django_db
def test_cannot_request_a_shift_that_is_not_public(client):
    hidden = _future_shift(AccountFactory(account_type="shelter"))
    res = client.post(f"/api/v1/shifts/{hidden.pk}/signups", {"waiver_accepted": True},
                      content_type="application/json", **hdr(AccountFactory()))
    assert res.status_code == 404


@pytest.mark.django_db
def test_verification_join_does_not_inflate_slot_counts(client):
    shelter = verified_shelter()
    from verifications.models import VerificationRequest
    VerificationRequest.objects.create(account=shelter, type="shelter_org", status="approved")
    s = _future_shift(shelter, capacity=3)
    VolunteerSignup.objects.create(shift=s, volunteer_account=AccountFactory(),
                                   status=SignupStatus.APPROVED)
    row = client.get("/api/v1/shifts").json()["results"][0]
    assert row["slots_left"] == 2


@pytest.mark.django_db
def test_a_shelter_account_cannot_request_a_shift(client):
    s = _future_shift(verified_shelter())
    other_shelter = verified_shelter()
    res = client.post(f"/api/v1/shifts/{s.pk}/signups", {"waiver_accepted": True},
                      content_type="application/json", **hdr(other_shelter))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "shelters_cannot_volunteer"


@pytest.mark.django_db
def test_a_started_shift_cannot_be_requested(client):
    start = tz.now() - tz.timedelta(minutes=30)
    s = VolunteerShift.objects.create(shelter_account=verified_shelter(), starts_at=start,
                                      ends_at=start + tz.timedelta(hours=2), capacity=2)
    res = client.post(f"/api/v1/shifts/{s.pk}/signups", {"waiver_accepted": True},
                      content_type="application/json", **hdr(AccountFactory()))
    assert res.status_code == 404
