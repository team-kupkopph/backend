"""P4 · shelter flow (review §4.2; test plan K8, K9, K14, K15, K16, K27)."""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.tests.helpers import hdr, verified_shelter

SHIFTS = "/api/v1/shelter/shifts"


def _shift(shelter, hours_out=48, duration=2, **kw):
    start = timezone.now() + timezone.timedelta(hours=hours_out)
    d = dict(shelter_account=shelter, starts_at=start,
             ends_at=start + timezone.timedelta(hours=duration), capacity=3,
             title="Morning dog walk", city="Marikina")
    d.update(kw)
    return VolunteerShift.objects.create(**d)


def _signup(shift, status=SignupStatus.APPROVED, vol=None):
    return VolunteerSignup.objects.create(shift=shift, volunteer_account=vol or AccountFactory(),
                                          status=status, waiver_accepted=True)


@pytest.mark.django_db
def test_upcoming_pages_and_never_hides_new_shifts(client):
    shelter = verified_shelter()
    for i in range(25):
        _shift(shelter, hours_out=-100 - i)                   # 25 past shifts
    newest = _shift(shelter, hours_out=24 * 30)
    body = client.get(f"{SHIFTS}?when=upcoming", **hdr(shelter)).json()
    assert [r["shift_id"] for r in body["results"]] == [str(newest.pk)]
    assert body["next"] is None


@pytest.mark.django_db
def test_past_is_newest_first_paged_and_counts_attendance_due(client):
    shelter = verified_shelter()
    shifts = [_shift(shelter, hours_out=-10 - i) for i in range(21)]
    _signup(shifts[0])                                          # approved, unmarked
    page1 = client.get(f"{SHIFTS}?when=past", **hdr(shelter)).json()
    assert page1["results"][0]["shift_id"] == str(shifts[0].pk)
    assert page1["results"][0]["attendance_due"] == 1
    assert page1["next"] == 2
    page2 = client.get(f"{SHIFTS}?when=past&page=2", **hdr(shelter)).json()
    assert len(page2["results"]) == 1 and page2["next"] is None


@pytest.mark.django_db
def test_browse_pages(client):
    shelter = verified_shelter()
    for i in range(21):
        _shift(shelter, hours_out=24 + i)
    body = client.get("/api/v1/shifts").json()
    assert len(body["results"]) == 20 and body["next"] == 2


def _patch(client, shift, **body):
    return client.patch(f"{SHIFTS}/{shift.pk}", body, content_type="application/json",
                        **hdr(shift.shelter_account))


@pytest.mark.django_db
def test_capacity_cannot_drop_below_approved(client):
    s = _shift(verified_shelter(), capacity=4)
    for _ in range(3):
        _signup(s)
    res = _patch(client, s, capacity=2)
    assert res.status_code == 409
    body = res.json()["error"]
    body.pop("request_id", None)          # US-E2 stamps this on every hand-built error body
    assert body == {"code": "capacity_below_approved",
                    "message": "3 volunteers are already confirmed",
                    "details": {"approved": 3}}


@pytest.mark.django_db
def test_raising_capacity_reopens_a_full_shift_and_lowering_to_approved_fills_it(client):
    s = _shift(verified_shelter(), capacity=1, status=ShiftStatus.FULL)
    _signup(s)
    assert _patch(client, s, capacity=3).json()["status"] == "open"
    assert _patch(client, s, capacity=1).json()["status"] == "full"


@pytest.mark.django_db
def test_an_ended_shift_cannot_be_edited_or_cancelled(client):
    s = _shift(verified_shelter(), hours_out=-5)
    assert _patch(client, s, capacity=9).json()["error"]["code"] == "shift_ended"
    res = client.post(f"{SHIFTS}/{s.pk}/cancel", **hdr(s.shelter_account))
    assert res.json()["error"]["code"] == "shift_ended"


@pytest.mark.django_db
def test_capacity_has_a_sane_ceiling(client):
    s = _shift(verified_shelter())
    res = _patch(client, s, capacity=3_000_000_000)
    assert res.status_code == 400 and res.json()["error"]["field"] == "capacity"
