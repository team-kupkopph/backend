import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.reliability import RELIABLE_MIN_COMPLETED, reliability_for


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shift(shelter=None, days=2, capacity=5):
    return VolunteerShift.objects.create(
        shelter_account=shelter or AccountFactory(account_type="shelter"),
        starts_at=timezone.now() + timezone.timedelta(days=days),
        ends_at=timezone.now() + timezone.timedelta(days=days, hours=2), capacity=capacity)


def _history(vol, outcomes):
    """outcomes: list of (days_offset, status) — days_offset orders the SHIFT time."""
    for offset, status in outcomes:
        VolunteerSignup.objects.create(shift=_shift(days=offset),
                                       volunteer_account=vol, status=status)


@pytest.mark.django_db
def test_a_brand_new_volunteer_is_not_reliable_and_not_flagged():
    r = reliability_for(AccountFactory())
    assert r == {"shifts_completed": 0, "no_shows": 0, "consecutive_no_shows": 0,
                 "needs_reapproval": False, "is_reliable": False}


@pytest.mark.django_db
def test_three_consecutive_no_shows_trips_the_gate():
    vol = AccountFactory()
    _history(vol, [(-5, SignupStatus.NO_SHOW), (-4, SignupStatus.NO_SHOW),
                   (-3, SignupStatus.NO_SHOW)])
    r = reliability_for(vol)
    assert r["consecutive_no_shows"] == 3
    assert r["needs_reapproval"] is True


@pytest.mark.django_db
def test_two_no_shows_do_not_trip_it():
    vol = AccountFactory()
    _history(vol, [(-5, SignupStatus.NO_SHOW), (-4, SignupStatus.NO_SHOW)])
    assert reliability_for(vol)["needs_reapproval"] is False


@pytest.mark.django_db
def test_a_completed_shift_resets_the_run():
    vol = AccountFactory()
    _history(vol, [(-5, SignupStatus.NO_SHOW), (-4, SignupStatus.NO_SHOW),
                   (-3, SignupStatus.NO_SHOW), (-2, SignupStatus.COMPLETED)])
    r = reliability_for(vol)
    assert r["consecutive_no_shows"] == 0
    assert r["needs_reapproval"] is False
    assert r["no_shows"] == 3          # lifetime total is still reported


@pytest.mark.django_db
def test_the_run_is_ordered_by_shift_time_not_booking_time():
    """⚠️ A volunteer can book a far-future shift before a near one. Attendance is a fact
    about when the shift HAPPENED, so the run must be computed over starts_at. Here the
    completed shift is created FIRST but occurs LAST — it must still reset the run."""
    vol = AccountFactory()
    VolunteerSignup.objects.create(shift=_shift(days=-1), volunteer_account=vol,
                                   status=SignupStatus.COMPLETED)   # created first, latest shift
    for offset in (-5, -4, -3):
        VolunteerSignup.objects.create(shift=_shift(days=offset), volunteer_account=vol,
                                       status=SignupStatus.NO_SHOW)
    r = reliability_for(vol)
    assert r["consecutive_no_shows"] == 0, "run must be ordered by shift starts_at"
    assert r["needs_reapproval"] is False


@pytest.mark.django_db
def test_cancellations_do_not_feed_the_signal():
    """A cancelled shift shows in history but must not drag the chip, or people stop
    cancelling honestly and simply no-show."""
    vol = AccountFactory()
    _history(vol, [(-5, SignupStatus.CANCELLED), (-4, SignupStatus.CANCELLED),
                   (-3, SignupStatus.CANCELLED)])
    r = reliability_for(vol)
    assert r["consecutive_no_shows"] == 0
    assert r["needs_reapproval"] is False


@pytest.mark.django_db
def test_reliable_requires_the_completed_floor():
    vol = AccountFactory()
    _history(vol, [(-i, SignupStatus.COMPLETED) for i in range(1, RELIABLE_MIN_COMPLETED)])
    assert reliability_for(vol)["is_reliable"] is False
    _history(vol, [(-10, SignupStatus.COMPLETED)])
    assert reliability_for(vol)["is_reliable"] is True


@pytest.mark.django_db
def test_requests_endpoint_carries_the_reliability_block(client):
    shelter = AccountFactory(account_type="shelter")
    shift = _shift(shelter=shelter)
    vol = AccountFactory()
    VolunteerSignup.objects.create(shift=shift, volunteer_account=vol)
    body = client.get(f"/api/v1/shelter/shifts/{shift.pk}/requests", **_hdr(shelter)).json()
    assert body["results"][0]["reliability"]["needs_reapproval"] is False


@pytest.mark.django_db
def test_approving_a_flagged_volunteer_needs_acknowledgement(client):
    shelter = AccountFactory(account_type="shelter")
    shift = _shift(shelter=shelter)
    vol = AccountFactory()
    _history(vol, [(-5, SignupStatus.NO_SHOW), (-4, SignupStatus.NO_SHOW),
                   (-3, SignupStatus.NO_SHOW)])
    su = VolunteerSignup.objects.create(shift=shift, volunteer_account=vol)

    res = client.post(f"/api/v1/shelter/signups/{su.pk}/approve", **_hdr(shelter))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "reapproval_required"
    su.refresh_from_db()
    assert su.status == SignupStatus.REQUESTED     # nothing mutated

    res = client.post(f"/api/v1/shelter/signups/{su.pk}/approve",
                      {"acknowledged_reapproval": True},
                      content_type="application/json", **_hdr(shelter))
    assert res.status_code == 200
    su.refresh_from_db()
    assert su.status == SignupStatus.APPROVED


@pytest.mark.django_db
def test_a_string_false_acknowledgement_does_not_clear_the_reapproval_gate(client):
    """M-2 · the gate must accept only a real JSON `true`. A truthy string like "false"
    read as acknowledged would silently skip the disclosure the shelter is meant to see."""
    shelter = AccountFactory(account_type="shelter")
    shift = _shift(shelter=shelter)
    vol = AccountFactory()
    _history(vol, [(-5, SignupStatus.NO_SHOW), (-4, SignupStatus.NO_SHOW),
                   (-3, SignupStatus.NO_SHOW)])
    su = VolunteerSignup.objects.create(shift=shift, volunteer_account=vol)

    res = client.post(f"/api/v1/shelter/signups/{su.pk}/approve",
                      {"acknowledged_reapproval": "false"},
                      content_type="application/json", **_hdr(shelter))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "reapproval_required"
    su.refresh_from_db()
    assert su.status == SignupStatus.REQUESTED     # nothing mutated


@pytest.mark.django_db
def test_requests_endpoint_query_count_is_bounded_regardless_of_pending_volunteers(
        client, django_assert_max_num_queries):
    """I-2 · the requests endpoint batches the reliability aggregates. The query count must
    NOT scale with the number of pending volunteers (the old per-row `reliability_for` was
    ~4 queries each). Ten pending volunteers, each with no-show history, must stay bounded."""
    shelter = AccountFactory(account_type="shelter")
    shift = _shift(shelter=shelter)
    for _ in range(10):
        vol = AccountFactory()
        _history(vol, [(-6, SignupStatus.NO_SHOW), (-5, SignupStatus.COMPLETED)])
        VolunteerSignup.objects.create(shift=shift, volunteer_account=vol)

    hdr = _hdr(shelter)
    with django_assert_max_num_queries(12):
        res = client.get(f"/api/v1/shelter/shifts/{shift.pk}/requests", **hdr)
    assert res.status_code == 200
    assert len(res.json()["results"]) == 10


@pytest.mark.django_db
def test_the_requests_payload_leaks_no_other_shelters_identity(client):
    """D-S5-2 · the count is global, the disclosure is not. A shelter sees four integers —
    never which other shelters, when, or a per-org breakdown."""
    other = AccountFactory(account_type="shelter", display_name="Some Other Shelter",
                           email="other-shelter@example.com")
    vol = AccountFactory()
    for offset in (-5, -4, -3):
        VolunteerSignup.objects.create(shift=_shift(shelter=other, days=offset),
                                       volunteer_account=vol, status=SignupStatus.NO_SHOW)

    viewing = AccountFactory(account_type="shelter")
    shift = _shift(shelter=viewing)
    VolunteerSignup.objects.create(shift=shift, volunteer_account=vol)

    text = client.get(f"/api/v1/shelter/shifts/{shift.pk}/requests",
                      **_hdr(viewing)).content.decode()
    assert "Some Other Shelter" not in text
    assert "other-shelter@example.com" not in text
    assert str(other.pk) not in text


@pytest.mark.django_db
def test_a_recent_no_show_suspends_reliable_until_the_next_completion():
    vol = AccountFactory()
    _history(vol, [(-50, SignupStatus.COMPLETED), (-40, SignupStatus.COMPLETED),
                   (-30, SignupStatus.COMPLETED), (-20, SignupStatus.NO_SHOW)])
    assert reliability_for(vol)["is_reliable"] is False
    _history(vol, [(-10, SignupStatus.COMPLETED)])
    assert reliability_for(vol)["is_reliable"] is True
