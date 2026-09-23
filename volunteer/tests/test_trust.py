"""P1 · trust & privacy fixes (dev/volunteer-build-review.md G3/G4/G16, test plan K1–K6)."""
import pytest
from datetime import timezone
from django.conf import settings
from django.utils import timezone as tz

from volunteer.models import VolunteerShift
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
    assert shift.starts_at.astimezone(timezone.utc).isoformat() == "2030-10-04T01:00:00+00:00"


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
