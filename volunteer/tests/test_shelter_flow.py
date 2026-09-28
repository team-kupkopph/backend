"""P4 · shelter flow (review §4.2; test plan K8, K9, K14, K15, K16, K27)."""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from notifications.models import Notification
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


@pytest.mark.django_db
def test_removing_one_volunteer_frees_the_slot_and_tells_them(client):
    s = _shift(verified_shelter(), capacity=1, status=ShiftStatus.FULL)
    su = _signup(s)
    res = client.post(f"/api/v1/shelter/signups/{su.pk}/remove", **hdr(s.shelter_account))
    assert res.status_code == 200
    su.refresh_from_db(); s.refresh_from_db()
    assert (su.status, su.cancelled_by, s.status) == ("cancelled", "shelter", "open")
    assert Notification.objects.filter(account=su.volunteer_account,
                                       type="signup_cancelled_by_shelter").exists()


@pytest.mark.django_db
def test_removal_does_not_count_against_the_volunteer(client):
    s = _shift(verified_shelter())
    su = _signup(s)
    client.post(f"/api/v1/shelter/signups/{su.pk}/remove", **hdr(s.shelter_account))
    from volunteer.reliability import reliability_for
    assert reliability_for(su.volunteer_account)["no_shows"] == 0


@pytest.mark.django_db
def test_attendance_can_be_undone_within_24_hours_only(client):
    s = _shift(verified_shelter(), hours_out=-5)
    su = _signup(s)
    client.post(f"/api/v1/shelter/signups/{su.pk}/attendance", {"outcome": "no_show"},
                content_type="application/json", **hdr(s.shelter_account))
    res = client.post(f"/api/v1/shelter/signups/{su.pk}/attendance/undo", **hdr(s.shelter_account))
    assert res.status_code == 200
    su.refresh_from_db()
    assert su.status == "approved" and su.attendance_marked_at is None
    client.post(f"/api/v1/shelter/signups/{su.pk}/attendance", {"outcome": "no_show"},
                content_type="application/json", **hdr(s.shelter_account))
    VolunteerSignup.objects.filter(pk=su.pk).update(
        attendance_marked_at=timezone.now() - timezone.timedelta(hours=25))
    res = client.post(f"/api/v1/shelter/signups/{su.pk}/attendance/undo", **hdr(s.shelter_account))
    assert res.json()["error"]["code"] == "undo_expired"


@pytest.mark.django_db
def test_roster_shows_check_in_animal_and_contact_flag(client):
    s = _shift(verified_shelter(), hours_out=-1)
    su = _signup(s)
    VolunteerSignup.objects.filter(pk=su.pk).update(check_in_at=timezone.now(),
                                                    contact_share_consent=True)
    row = client.get(f"{SHIFTS}/{s.pk}/roster", **hdr(s.shelter_account)).json()["results"][0]
    assert row["check_in_at"] is not None
    assert row["contact_shared"] is True
    assert row["assigned_animal"] is None and row["can_undo"] is False


@pytest.mark.django_db
def test_another_shelter_cannot_remove_undo_or_reassign(client):
    s = _shift(verified_shelter())
    su = _signup(s)
    other = verified_shelter()
    for path in (f"/api/v1/shelter/signups/{su.pk}/remove",
                 f"/api/v1/shelter/signups/{su.pk}/attendance/undo"):
        assert client.post(path, **hdr(other)).status_code == 403
    assert client.patch(f"/api/v1/shelter/signups/{su.pk}", {"assigned_listing_id": None},
                        content_type="application/json", **hdr(other)).status_code == 403


@pytest.mark.django_db
def test_the_shelter_can_change_or_clear_the_assigned_animal(client):
    from listings.models import AdoptionListing
    shelter = verified_shelter()
    s = _shift(shelter, type="walking")
    su = _signup(s)
    mine = AdoptionListing.objects.create(posted_by=shelter, name="Bruno", species="dog",
                                          city="Marikina", adoption_fee="0")
    theirs = AdoptionListing.objects.create(posted_by=verified_shelter(), name="Milo",
                                            species="dog", city="Marikina", adoption_fee="0")
    url = f"/api/v1/shelter/signups/{su.pk}"
    ok = client.patch(url, {"assigned_listing_id": str(mine.pk)},
                      content_type="application/json", **hdr(shelter))
    assert ok.json()["assigned_animal"]["name"] == "Bruno"
    bad = client.patch(url, {"assigned_listing_id": str(theirs.pk)},
                       content_type="application/json", **hdr(shelter))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_listing"
    cleared = client.patch(url, {"assigned_listing_id": None},
                           content_type="application/json", **hdr(shelter))
    assert cleared.json()["assigned_animal"] is None


@pytest.mark.django_db
def test_requests_say_whether_the_volunteer_is_a_verified_member(client):
    from verifications.models import AccountCapability
    s = _shift(verified_shelter())
    member = AccountFactory()
    AccountCapability.objects.create(account=member, capability="rescuer", status="approved")
    _signup(s, status=SignupStatus.REQUESTED, vol=member)
    row = client.get(f"{SHIFTS}/{s.pk}/requests", **hdr(s.shelter_account)).json()["results"][0]
    assert row["is_verified_member"] is True
    assert set(row["reliability"]) == {"shifts_completed", "no_shows", "consecutive_no_shows",
                                       "needs_reapproval", "is_reliable"}
