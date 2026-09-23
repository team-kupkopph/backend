import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from notifications.models import Notification
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.tests.helpers import verified_shelter

SHIFTS = "/api/v1/shelter/shifts"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter():
    return verified_shelter()


def _payload(**kw):
    d = dict(type="walking",
             starts_at=(timezone.now() + timezone.timedelta(days=2)).isoformat(),
             ends_at=(timezone.now() + timezone.timedelta(days=2, hours=2)).isoformat(),
             capacity=3)
    d.update(kw)
    return d


@pytest.mark.django_db
def test_shelter_creates_an_open_shift(client):
    res = client.post(SHIFTS, _payload(), content_type="application/json", **_hdr(_shelter()))
    assert res.status_code == 201
    assert res.json()["status"] == "open"


@pytest.mark.django_db
def test_a_non_shelter_account_cannot_post_a_shift(client):
    res = client.post(SHIFTS, _payload(), content_type="application/json",
                      **_hdr(AccountFactory(account_type="personal")))
    assert res.status_code == 403


@pytest.mark.django_db
def test_guest_cannot_post_a_shift(client):
    res = client.post(SHIFTS, _payload(), content_type="application/json")
    assert res.status_code == 401


@pytest.mark.django_db
def test_ends_before_starts_is_refused(client):
    now = timezone.now()
    res = client.post(SHIFTS, _payload(
        starts_at=(now + timezone.timedelta(hours=3)).isoformat(),
        ends_at=(now + timezone.timedelta(hours=1)).isoformat()),
        content_type="application/json", **_hdr(_shelter()))
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "bad_window"


@pytest.mark.django_db
def test_list_returns_only_my_own_shifts(client):
    mine, theirs = _shelter(), _shelter()
    client.post(SHIFTS, _payload(), content_type="application/json", **_hdr(mine))
    client.post(SHIFTS, _payload(), content_type="application/json", **_hdr(theirs))
    body = client.get(SHIFTS, **_hdr(mine)).json()
    assert len(body["results"]) == 1


@pytest.mark.django_db
def test_list_includes_org_name(client):
    mine = _shelter()
    client.post(SHIFTS, _payload(), content_type="application/json", **_hdr(mine))
    body = client.get(SHIFTS, **_hdr(mine)).json()
    assert body["results"][0]["org_name"] == mine.display_name


@pytest.mark.django_db
def test_list_query_count_is_bounded_regardless_of_shift_count(
        client, django_assert_max_num_queries):
    """Task 4b fix round 1 · `_shift_repr` now reads `shift.shelter_account.display_name`
    for every row, so this queryset must `select_related("shelter_account")` — otherwise the
    shelter's own shift list regresses into a per-row N+1. Query count must stay flat as the
    number of listed shifts grows, same guard as `test_browse_query_count_is_bounded_...`."""
    mine = _shelter()
    for _ in range(10):
        client.post(SHIFTS, _payload(), content_type="application/json", **_hdr(mine))
    with django_assert_max_num_queries(4):
        res = client.get(SHIFTS, **_hdr(mine))
    assert res.status_code == 200
    assert len(res.json()["results"]) == 10


@pytest.mark.django_db
def test_a_shelter_cannot_edit_another_shelters_shift(client):
    owner = _shelter()
    sid = client.post(SHIFTS, _payload(), content_type="application/json",
                      **_hdr(owner)).json()["shift_id"]
    res = client.patch(f"{SHIFTS}/{sid}", {"capacity": 9},
                       content_type="application/json", **_hdr(_shelter()))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "not_your_shift"


@pytest.mark.django_db
def test_a_foreign_shelter_cannot_cancel_another_shelters_shift(client):
    owner = _shelter()
    sid = client.post(SHIFTS, _payload(), content_type="application/json",
                      **_hdr(owner)).json()["shift_id"]
    res = client.post(f"{SHIFTS}/{sid}/cancel", **_hdr(_shelter()))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "not_your_shift"


@pytest.mark.django_db
def test_cancelling_an_already_closed_shift_is_refused(client):
    owner = _shelter()
    sid = client.post(SHIFTS, _payload(), content_type="application/json",
                      **_hdr(owner)).json()["shift_id"]
    first = client.post(f"{SHIFTS}/{sid}/cancel", **_hdr(owner))
    assert first.status_code == 200
    res = client.post(f"{SHIFTS}/{sid}/cancel", **_hdr(owner))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "shift_closed"


@pytest.mark.django_db
def test_bad_window_is_enforced_on_patch(client):
    owner = _shelter()
    sid = client.post(SHIFTS, _payload(), content_type="application/json",
                      **_hdr(owner)).json()["shift_id"]
    now = timezone.now()
    res = client.patch(f"{SHIFTS}/{sid}",
                       {"starts_at": (now + timezone.timedelta(days=3)).isoformat(),
                        "ends_at": (now + timezone.timedelta(days=1)).isoformat()},
                       content_type="application/json", **_hdr(owner))
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "bad_window"


@pytest.mark.django_db
def test_cancel_cascade_skips_non_live_signups(client):
    owner = _shelter()
    sid = client.post(SHIFTS, _payload(), content_type="application/json",
                      **_hdr(owner)).json()["shift_id"]
    shift = VolunteerShift.objects.get(pk=sid)
    approved, declined, cancelled = AccountFactory(), AccountFactory(), AccountFactory()
    VolunteerSignup.objects.create(shift=shift, volunteer_account=approved,
                                   status=SignupStatus.APPROVED)
    VolunteerSignup.objects.create(shift=shift, volunteer_account=declined,
                                   status=SignupStatus.DECLINED)
    VolunteerSignup.objects.create(shift=shift, volunteer_account=cancelled,
                                   status=SignupStatus.CANCELLED)

    res = client.post(f"{SHIFTS}/{sid}/cancel", **_hdr(owner))

    assert res.status_code == 200
    assert res.json()["cancelled_signups"] == 1
    assert VolunteerSignup.objects.get(shift=shift,
                                       volunteer_account=declined).status == SignupStatus.DECLINED
    assert VolunteerSignup.objects.get(
        shift=shift, volunteer_account=cancelled).status == SignupStatus.CANCELLED
    assert Notification.objects.filter(type="shift_cancelled_by_shelter").count() == 1


@pytest.mark.django_db
def test_cancel_cascades_to_signups_and_notifies_each_volunteer(client):
    owner = _shelter()
    sid = client.post(SHIFTS, _payload(), content_type="application/json",
                      **_hdr(owner)).json()["shift_id"]
    shift = VolunteerShift.objects.get(pk=sid)
    v1, v2 = AccountFactory(), AccountFactory()
    for v in (v1, v2):
        VolunteerSignup.objects.create(shift=shift, volunteer_account=v,
                                       status=SignupStatus.APPROVED)

    res = client.post(f"{SHIFTS}/{sid}/cancel", **_hdr(owner))

    assert res.status_code == 200
    assert res.json()["cancelled_signups"] == 2
    shift.refresh_from_db()
    assert shift.status == ShiftStatus.CLOSED
    for v in (v1, v2):
        su = VolunteerSignup.objects.get(shift=shift, volunteer_account=v)
        assert su.status == SignupStatus.CANCELLED
        assert su.cancelled_at is not None
        assert Notification.objects.filter(
            account=v, type="shift_cancelled_by_shelter").count() == 1


@pytest.mark.django_db
def test_closed_is_terminal(client):
    owner = _shelter()
    sid = client.post(SHIFTS, _payload(), content_type="application/json",
                      **_hdr(owner)).json()["shift_id"]
    client.post(f"{SHIFTS}/{sid}/cancel", **_hdr(owner))
    res = client.patch(f"{SHIFTS}/{sid}", {"capacity": 5},
                       content_type="application/json", **_hdr(owner))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "shift_closed"
