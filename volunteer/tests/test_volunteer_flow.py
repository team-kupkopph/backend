"""P3 · volunteer flow (review §4.1, D4; test plan K4, K7, K12, K13, K17, K21)."""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from volunteer.models import VolunteerShift, VolunteerSignup
from volunteer.tests.helpers import hdr, verified_shelter


def _shift(hours_out=48, duration=2, **kw):
    start = timezone.now() + timezone.timedelta(hours=hours_out)
    d = dict(shelter_account=verified_shelter(), starts_at=start,
             ends_at=start + timezone.timedelta(hours=duration), capacity=3,
             title="Morning dog walk", city="Marikina")
    d.update(kw)
    return VolunteerShift.objects.create(**d)


def _request(client, shift, vol):
    return client.post(f"/api/v1/shifts/{shift.pk}/signups", {"waiver_accepted": True},
                       content_type="application/json", **hdr(vol))


@pytest.mark.django_db
def test_can_request_again_after_cancelling(client):
    s, vol = _shift(), AccountFactory()
    first = _request(client, s, vol).json()["signup_id"]
    assert client.post(f"/api/v1/signups/{first}/cancel", **hdr(vol)).status_code == 200
    again = _request(client, s, vol)
    assert again.status_code == 201
    assert again.json()["signup_id"] != first          # a new row; the cancel stays on record
    assert VolunteerSignup.objects.filter(shift=s, volunteer_account=vol).count() == 2


@pytest.mark.django_db
def test_can_request_again_after_one_decline_and_the_shelter_is_told(client):
    s, vol = _shift(), AccountFactory()
    first = _request(client, s, vol).json()["signup_id"]
    client.post(f"/api/v1/shelter/signups/{first}/decline", **hdr(s.shelter_account))
    assert _request(client, s, vol).status_code == 201
    rows = client.get(f"/api/v1/shelter/shifts/{s.pk}/requests",
                      **hdr(s.shelter_account)).json()["results"]
    assert rows[0]["previously_declined"] is True


@pytest.mark.django_db
def test_a_second_decline_is_final(client):
    s, vol = _shift(), AccountFactory()
    for _ in range(2):
        sid = _request(client, s, vol).json()["signup_id"]
        client.post(f"/api/v1/shelter/signups/{sid}/decline", **hdr(s.shelter_account))
    res = _request(client, s, vol)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "declined_twice"


@pytest.mark.django_db
def test_two_live_requests_still_collide(client):
    s, vol = _shift(), AccountFactory()
    assert _request(client, s, vol).status_code == 201
    res = _request(client, s, vol)
    assert res.status_code == 409 and res.json()["error"]["code"] == "already_requested"
