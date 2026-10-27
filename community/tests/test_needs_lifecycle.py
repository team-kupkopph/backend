"""Abot-tulong wishlist refactor · lifecycle (Task 3 schema; Task 4 adds the behaviour tests).

Schema first: the columns the refactor adds, and the CHECKs that keep them honest even if a
writer is bypassed.
"""
import pytest
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.factories import AccountFactory
from community.models import NeedPledge, NeedStatus, PledgeStatus, ShelterNeed
from community.needs import settle_stranded_pledges
from community.tests.test_needs import _hdr, _need, _pledge, _shelter
from community.tests.test_needs_integrity import _race, _race_pair
from notifications.models import Notification
from shelter.models import ShelterProfile


@pytest.mark.django_db
def test_the_refactor_columns_exist_with_their_checks():
    need = _need()
    assert need.unit == ""
    assert need.fulfilled_at is None

    pledge = _pledge(need)
    assert pledge.quantity_received is None
    assert pledge.decided_at is None
    assert pledge.cancel_reason is None
    assert pledge.note == ""

    # quantity_received is NULL or >= 1
    with pytest.raises(IntegrityError), transaction.atomic():
        NeedPledge.objects.create(need=need, pledger_account=AccountFactory(), quantity=1,
                                  quantity_received=0)
    # cancel_reason is NULL or one of donor / need_closed
    with pytest.raises(IntegrityError), transaction.atomic():
        NeedPledge.objects.create(need=need, pledger_account=AccountFactory(), quantity=1,
                                  status=PledgeStatus.CANCELLED, cancel_reason="x")

    # the valid values save
    ok = NeedPledge.objects.create(need=need, pledger_account=AccountFactory(), quantity=2,
                                   quantity_received=1, cancel_reason="need_closed",
                                   note="left at the gate")
    ok.refresh_from_db()
    assert (ok.quantity_received, ok.cancel_reason, ok.note) == (1, "need_closed", "left at the gate")
    NeedPledge.objects.create(need=need, pledger_account=AccountFactory(), quantity=1,
                              cancel_reason="donor")

    # the composite index is what the aggregates in Task 5 lean on
    names = {i.name for i in NeedPledge._meta.indexes}
    assert {"idx_need_pledge_need", "idx_need_pledge_pledger", "idx_need_pledge_need_status"} <= names


@pytest.mark.django_db
def test_shelter_profile_donation_instructions_defaults_empty():
    sh = _shelter()
    profile = ShelterProfile.objects.create(account=sh, org_name="Paws", org_type="shelter")
    assert profile.donation_instructions == ""


# --- Task 4 · the lifecycle: Mark complete, the cascade, the two new notifications --------

def _post(acc, path, body=None):
    return Client().post(path, body or {}, content_type="application/json", **_hdr(acc))


def _patch(acc, need, body):
    return Client().patch(f"/api/v1/needs/{need.pk}", body, content_type="application/json",
                          **_hdr(acc))


def _complete(need):
    return _post(need.shelter_account, f"/api/v1/needs/{need.pk}/complete", {})


@pytest.mark.django_db
def test_complete_below_target_closes_and_cancels_open_pledges():
    need = _need(needed=10, received=4)
    p1 = _pledge(need)
    p2 = _pledge(need, status=PledgeStatus.DELIVERED)
    res = _complete(need)
    assert res.status_code == 200
    assert res.json() == {"need_status": "closed", "cancelled_pledges": 1}
    p1.refresh_from_db()
    p2.refresh_from_db()
    need.refresh_from_db()
    assert (p1.status, p1.cancel_reason) == ("cancelled", "need_closed")
    assert p1.decided_at is not None
    assert p2.status == "delivered" and p2.cancel_reason is None
    assert need.status == NeedStatus.CLOSED and need.fulfilled_at is None
    note = Notification.objects.get(account=p1.pledger_account, type="need_closed")
    assert note.title == "Need closed"
    assert note.body == (f"{need.shelter_account.display_name} closed “{need.title}” — "
                         "your pledge was cancelled. Salamat for offering.")
    assert note.data == {"need_id": str(need.pk), "pledge_id": str(p1.pk)}
    # the delivered pledger is not told anything
    assert not Notification.objects.filter(account=p2.pledger_account).exists()


@pytest.mark.django_db
def test_complete_at_target_is_fulfilled_and_stamps_fulfilled_at():
    need = _need(needed=5, received=5)
    res = _complete(need)
    assert res.json() == {"need_status": "fulfilled", "cancelled_pledges": 0}
    need.refresh_from_db()
    assert need.status == NeedStatus.FULFILLED and need.fulfilled_at is not None


@pytest.mark.django_db
def test_complete_twice_is_idempotent_and_silent():
    need = _need(needed=10, received=2)
    _pledge(need)
    first = _complete(need)
    assert first.json() == {"need_status": "closed", "cancelled_pledges": 1}
    before = Notification.objects.count()
    second = _complete(need)
    assert second.status_code == 200
    assert second.json() == {"need_status": "closed", "cancelled_pledges": 0}
    assert Notification.objects.count() == before


@pytest.mark.django_db
def test_complete_with_no_pledges_sends_nothing():
    need = _need(needed=10, received=2)
    res = _complete(need)
    assert res.json() == {"need_status": "closed", "cancelled_pledges": 0}
    assert Notification.objects.count() == 0


@pytest.mark.django_db
def test_complete_on_a_fulfilled_need_cancels_the_leftover_pledges():
    """Backlog · a fulfilled need still takes late deliveries (R1), so a pledge left over after the
    flip used to sit "to confirm" for ever. Mark complete now settles it: the need stays
    fulfilled (it did get everything), the leftover is cancelled and its donor told."""
    need = _need(needed=3, received=3, status=NeedStatus.FULFILLED)
    ShelterNeed.objects.filter(pk=need.pk).update(fulfilled_at=timezone.now())
    need.refresh_from_db()
    stamped = need.fulfilled_at
    leftover = _pledge(need)
    delivered = _pledge(need, status=PledgeStatus.DELIVERED)
    res = _complete(need)
    assert res.status_code == 200
    assert res.json() == {"need_status": "fulfilled", "cancelled_pledges": 1}
    leftover.refresh_from_db()
    delivered.refresh_from_db()
    need.refresh_from_db()
    assert (leftover.status, leftover.cancel_reason) == ("cancelled", "need_closed")
    assert leftover.decided_at is not None
    assert delivered.status == "delivered" and delivered.cancel_reason is None
    assert need.status == NeedStatus.FULFILLED and need.fulfilled_at == stamped
    note = Notification.objects.get(account=leftover.pledger_account, type="need_closed")
    assert note.data == {"need_id": str(need.pk), "pledge_id": str(leftover.pk)}
    assert not Notification.objects.filter(account=delivered.pledger_account).exists()


@pytest.mark.django_db
def test_complete_on_a_fulfilled_need_twice_is_silent():
    need = _need(needed=3, received=3, status=NeedStatus.FULFILLED)
    _pledge(need)
    assert _complete(need).json() == {"need_status": "fulfilled", "cancelled_pledges": 1}
    before = Notification.objects.count()
    assert _complete(need).json() == {"need_status": "fulfilled", "cancelled_pledges": 0}
    assert Notification.objects.count() == before


@pytest.mark.django_db
def test_complete_on_a_closed_need_changes_nothing():
    """`closed` is terminal: Mark complete leaves it, and anything still on it, alone. A pledge
    stranded on a need closed before the refactor is settled once, by the data migration."""
    need = _need(needed=10, received=4, status=NeedStatus.CLOSED)
    orphan = _pledge(need)
    res = _complete(need)
    assert res.json() == {"need_status": "closed", "cancelled_pledges": 0}
    orphan.refresh_from_db()
    assert orphan.status == PledgeStatus.PLEDGED
    assert Notification.objects.count() == 0


@pytest.mark.django_db
def test_complete_is_the_owners_only():
    need = _need()
    assert _post(_shelter(), f"/api/v1/needs/{need.pk}/complete").status_code == 403
    assert _post(AccountFactory(), f"/api/v1/needs/{need.pk}/complete").status_code == 403
    assert Client().post(f"/api/v1/needs/{need.pk}/complete").status_code == 401
    res = _post(need.shelter_account, "/api/v1/needs/00000000-0000-4000-8000-000000000000/complete")
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"
    need.refresh_from_db()
    assert need.status == NeedStatus.OPEN


@pytest.mark.django_db
def test_patch_close_routes_to_complete():
    """Old app builds still send PATCH {status: "closed"}; the cascade must happen for them."""
    need = _need(needed=10, received=4)
    p = _pledge(need)
    res = _patch(need.shelter_account, need, {"status": "closed"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "closed" and body["need_id"] == str(need.pk)
    assert {"title", "category", "description", "quantity_needed", "quantity_received"} <= set(body)
    p.refresh_from_db()
    assert (p.status, p.cancel_reason) == ("cancelled", "need_closed")
    assert Notification.objects.filter(account=p.pledger_account, type="need_closed").count() == 1


@pytest.mark.django_db
def test_patch_close_at_target_resolves_fulfilled():
    need = _need(needed=5, received=5)
    res = _patch(need.shelter_account, need, {"status": "closed"})
    assert res.status_code == 200 and res.json()["status"] == "fulfilled"


@pytest.mark.django_db
def test_patch_close_on_a_fulfilled_need_keeps_fulfilled():
    need = _need(needed=3, received=3, status=NeedStatus.FULFILLED)
    res = _patch(need.shelter_account, need, {"status": "closed"})
    assert res.status_code == 200 and res.json()["status"] == "fulfilled"
    need.refresh_from_db()
    assert need.status == NeedStatus.FULFILLED


@pytest.mark.django_db
def test_patch_close_on_a_fulfilled_need_settles_its_leftover_pledges_too():
    """Old app builds still send PATCH {status: "closed"}; it is Mark complete, so it settles a
    fulfilled need's leftovers the same way."""
    need = _need(needed=3, received=3, status=NeedStatus.FULFILLED)
    p = _pledge(need)
    res = _patch(need.shelter_account, need, {"status": "closed"})
    assert res.status_code == 200 and res.json()["status"] == "fulfilled"
    p.refresh_from_db()
    assert (p.status, p.cancel_reason) == ("cancelled", "need_closed")
    assert Notification.objects.filter(account=p.pledger_account, type="need_closed").count() == 1


@pytest.mark.django_db
def test_patch_close_on_a_closed_need_is_idempotent():
    need = _need(status=NeedStatus.CLOSED)
    res = _patch(need.shelter_account, need, {"status": "closed"})
    assert res.status_code == 200 and res.json()["status"] == "closed"
    assert Notification.objects.count() == 0


@pytest.mark.django_db
def test_patch_keeps_its_owner_and_missing_messages():
    need = _need()
    res = _patch(_shelter(), need, {"title": "x"})
    assert res.status_code == 403 and res.json()["error"]["code"] == "forbidden"
    assert res.json()["error"]["message"] == "Only the owning shelter can edit this need."
    res = Client().patch("/api/v1/needs/00000000-0000-4000-8000-000000000000", {"title": "x"},
                         content_type="application/json", **_hdr(need.shelter_account))
    assert res.status_code == 404 and res.json()["error"]["message"] == "No such need."


@pytest.mark.django_db
def test_editing_a_non_open_need_is_409_need_not_open():
    for status in (NeedStatus.CLOSED, NeedStatus.FULFILLED):
        need = _need(status=status)
        res = _patch(need.shelter_account, need, {"title": "x"})
        assert res.status_code == 409 and res.json()["error"]["code"] == "need_not_open"
        assert res.json()["error"]["message"] == "Only an open need can be edited."


@pytest.mark.django_db
def test_target_below_received_is_422():
    need = _need(needed=20, received=8)
    res = _patch(need.shelter_account, need, {"quantity_needed": 7})
    assert res.status_code == 422
    err = res.json()["error"]
    assert err["code"] == "below_received"
    assert err["details"] == {"quantity_received": 8}
    assert "8" in err["message"]
    need.refresh_from_db()
    assert need.quantity_needed == 20
    # exactly the received count is allowed
    assert _patch(need.shelter_account, need, {"quantity_needed": 8}).status_code == 200


@pytest.mark.django_db
def test_category_and_unit_are_editable_but_not_to_funds():
    need = _need()
    res = _patch(need.shelter_account, need, {"category": "medicine", "unit": "boxes"})
    assert res.status_code == 200 and res.json()["category"] == "medicine"
    need.refresh_from_db()
    assert (need.category, need.unit) == ("medicine", "boxes")

    res = _patch(need.shelter_account, need, {"category": "funds"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_category"
    assert res.json()["error"]["field"] == "category"
    need.refresh_from_db()
    assert need.category == "medicine"


@pytest.mark.django_db
def test_create_accepts_unit_and_refuses_funds():
    sh = _shelter()
    path = f"/api/v1/shelters/{sh.pk}/needs"
    res = _post(sh, path, {"title": "Rice", "category": "food", "quantity_needed": 3,
                           "unit": "sacks"})
    assert res.status_code == 201
    assert ShelterNeed.objects.get(pk=res.json()["need_id"]).unit == "sacks"
    res = _post(sh, path, {"title": "Cash", "category": "funds", "quantity_needed": 3})
    assert res.status_code == 400 and res.json()["error"]["code"] == "bad_category"
    res = _post(sh, path, {"title": "Rice", "category": "food", "unit": "x" * 17})
    assert res.status_code == 400 and res.json()["error"]["field"] == "unit"


@pytest.mark.django_db
def test_donor_cancel_notifies_the_shelter():
    need = _need(shelter=_shelter(display_name="Happy Paws"))
    giver = AccountFactory(display_name="Rina")
    p = _pledge(need, pledger=giver)
    res = _post(giver, f"/api/v1/pledges/{p.pk}/cancel")
    assert res.status_code == 200 and res.json() == {"status": "cancelled"}
    p.refresh_from_db()
    assert (p.status, p.cancel_reason) == ("cancelled", "donor") and p.decided_at is not None
    note = Notification.objects.get(account=need.shelter_account, type="pledge_cancelled")
    assert note.title == "Pledge cancelled"
    assert note.body == f"Rina cancelled their pledge to “{need.title}”."
    assert note.data == {"need_id": str(need.pk), "pledge_id": str(p.pk)}


@pytest.mark.django_db
def test_a_refused_cancel_notifies_nobody():
    need = _need()
    giver = AccountFactory()
    p = _pledge(need, pledger=giver, status=PledgeStatus.DELIVERED)
    assert _post(giver, f"/api/v1/pledges/{p.pk}/cancel").status_code == 409
    assert Notification.objects.count() == 0


@pytest.mark.django_db
def test_receive_records_the_actual_quantity_on_the_pledge():
    need = _need(needed=20)
    p = _pledge(need, quantity=10)
    res = _post(need.shelter_account, f"/api/v1/needs/{need.pk}/received",
                {"pledge_id": str(p.pk), "quantity_received": 6})
    assert res.status_code == 200
    p.refresh_from_db()
    need.refresh_from_db()
    assert p.status == PledgeStatus.DELIVERED and p.quantity_received == 6
    assert p.quantity == 10 and p.decided_at is not None
    assert need.quantity_received == 6 and need.fulfilled_at is None


@pytest.mark.django_db
def test_the_flip_to_fulfilled_stamps_fulfilled_at_once():
    need = _need(needed=5)
    p1, p2 = _pledge(need, quantity=5), _pledge(need, quantity=2)
    path = f"/api/v1/needs/{need.pk}/received"
    _post(need.shelter_account, path, {"pledge_id": str(p1.pk), "quantity_received": 5})
    need.refresh_from_db()
    stamp = need.fulfilled_at
    assert need.status == NeedStatus.FULFILLED and stamp is not None
    # a late delivery on the fulfilled need counts but does not move the stamp
    _post(need.shelter_account, path, {"pledge_id": str(p2.pk), "quantity_received": 2})
    need.refresh_from_db()
    assert need.quantity_received == 7 and need.fulfilled_at == stamp


@pytest.mark.django_db
def test_pledge_confirmed_names_the_shelter():
    need = _need(shelter=_shelter(display_name="Happy Paws"))
    p = _pledge(need)
    _post(need.shelter_account, f"/api/v1/needs/{need.pk}/received",
          {"pledge_id": str(p.pk), "quantity_received": 1})
    note = Notification.objects.get(account=p.pledger_account, type="pledge_confirmed")
    assert note.title == "Pledge received"
    assert note.body == f"Happy Paws received your pledge to “{need.title}”. Salamat!"


@pytest.mark.django_db
def test_a_pledge_note_is_stored_but_never_read_back_publicly():
    need = _need()
    giver = AccountFactory()
    res = _post(giver, f"/api/v1/needs/{need.pk}/pledges",
                {"quantity": 2, "note": "Dropping off Saturday"})
    assert res.status_code == 201
    pledge = NeedPledge.objects.get(pk=res.json()["pledge_id"])
    assert pledge.note == "Dropping off Saturday"
    mine = Client().get("/api/v1/me/pledges", **_hdr(giver)).json()["results"][0]
    assert "note" not in mine
    # default stays empty; over-long is refused
    assert _post(giver, f"/api/v1/needs/{need.pk}/pledges", {"quantity": 1}).status_code == 201
    assert NeedPledge.objects.filter(need=need, note="").count() == 1
    res = _post(giver, f"/api/v1/needs/{need.pk}/pledges", {"quantity": 1, "note": "x" * 281})
    assert res.status_code == 400 and res.json()["error"]["field"] == "note"


def test_the_two_new_types_are_registered():
    from notifications.types import REGISTRY
    for key, link in (("pledge_cancelled", "kupkop://shelter/needs/{need_id}/pledges"),
                      ("need_closed", "kupkop://donations")):
        t = REGISTRY[key]
        assert (t.data_shape, t.push, t.deep_link) == ("{need_id, pledge_id}", True, link)


# --- the races (real endpoints, real threads) ---------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_donor_cancel_racing_complete_has_one_reason():
    for _ in range(10):
        need = _need(needed=100, received=1)
        p = _pledge(need)
        _race_pair(lambda: _post(p.pledger_account, f"/api/v1/pledges/{p.pk}/cancel"),
                   lambda: _complete(need))
        p.refresh_from_db()
        need.refresh_from_db()
        assert p.status == PledgeStatus.CANCELLED
        assert p.cancel_reason in {"donor", "need_closed"} and p.decided_at is not None
        assert need.status == NeedStatus.CLOSED
        types = set(Notification.objects.filter(data__pledge_id=str(p.pk))
                    .values_list("type", flat=True))
        want = {"donor": "pledge_cancelled", "need_closed": "need_closed"}[p.cancel_reason]
        assert types == {want}, (p.cancel_reason, types)


@pytest.mark.django_db(transaction=True)
def test_a_pledge_racing_a_close_is_cascaded_or_refused_never_stranded():
    for _ in range(10):
        need = _need(needed=100, received=1)
        giver = AccountFactory()
        out = _race_pair(lambda: _post(giver, f"/api/v1/needs/{need.pk}/pledges", {"quantity": 1}),
                         lambda: _patch(need.shelter_account, need, {"status": "closed"}))
        need.refresh_from_db()
        assert out[1] == 200 and need.status == NeedStatus.CLOSED
        assert not NeedPledge.objects.filter(need=need, status=PledgeStatus.PLEDGED).exists()
        rows = list(NeedPledge.objects.filter(need=need))
        if out[0] == 201:   # the pledge landed first and the close cascaded it
            assert [(r.status, r.cancel_reason) for r in rows] == [("cancelled", "need_closed")]
            assert Notification.objects.filter(account=giver, type="need_closed").count() == 1
        else:               # the close landed first
            assert out[0] == 409 and rows == []
            assert not Notification.objects.filter(account=giver).exists()


@pytest.mark.django_db(transaction=True)
def test_a_confirm_racing_a_title_edit_keeps_the_confirmed_total():
    for i in range(10):
        need = _need(needed=100)
        p = _pledge(need, quantity=3)
        out = _race_pair(
            lambda: _post(need.shelter_account, f"/api/v1/needs/{need.pk}/received",
                          {"pledge_id": str(p.pk), "quantity_received": 3}),
            lambda: _patch(need.shelter_account, need,
                           {"title": f"Kibble {i}", "quantity_needed": 50, "category": "other"}))
        assert out == [200, 200]
        need.refresh_from_db()
        assert need.quantity_received == 3 and need.title == f"Kibble {i}"
        assert need.quantity_needed == 50


@pytest.mark.django_db(transaction=True)
def test_a_confirm_racing_a_target_edit_below_the_total_resolves_one_way():
    """received 2 + confirm of 3 vs an edit to 4: either the edit lands first (needed 4, then the
    confirm takes the total to 5, fulfilled) or the confirm lands first (total 5, edit is 422)."""
    for _ in range(10):
        need = _need(needed=100, received=2)
        p = _pledge(need, quantity=3)
        out = _race_pair(
            lambda: _post(need.shelter_account, f"/api/v1/needs/{need.pk}/received",
                          {"pledge_id": str(p.pk), "quantity_received": 3}),
            lambda: _patch(need.shelter_account, need, {"quantity_needed": 4}))
        need.refresh_from_db()
        assert out[0] == 200 and need.quantity_received == 5
        if out[1] == 200:
            assert need.quantity_needed == 4 and need.status == NeedStatus.FULFILLED
        else:
            assert out[1] == 422 and need.quantity_needed == 100


# --- fix round 1 -------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_cascade_locks_only_pledge_rows_not_the_donor_accounts():
    """`select_for_update` over a `select_related` join locks the joined `account` rows too —
    a third table no other writer locks. The cascade must say `OF "need_pledge"`."""
    from community.needs import complete_need
    need = _need(needed=10, received=1)
    _pledge(need)
    with CaptureQueriesContext(connection) as ctx:
        complete_need(need.pk)
    locking = [q["sql"] for q in ctx.captured_queries if "need_pledge" in q["sql"]
               and "FOR UPDATE" in q["sql"]]
    assert locking, "the cascade took no pledge locks at all"
    for sql in locking:
        assert 'FOR UPDATE OF "need_pledge"' in sql, sql


@pytest.mark.django_db(transaction=True)
def test_two_needs_sharing_donors_in_opposite_order_complete_without_deadlock():
    donors = [AccountFactory() for _ in range(6)]
    for _ in range(5):
        a, b = _need(needed=100, received=1), _need(needed=100, received=1)
        for d in donors:
            _pledge(a, pledger=d)
        for d in reversed(donors):
            _pledge(b, pledger=d)
        out = _race_pair(lambda: _complete(a), lambda: _complete(b))
        assert out == [200, 200]
        for need in (a, b):
            need.refresh_from_db()
            assert need.status == NeedStatus.CLOSED
            assert not NeedPledge.objects.filter(need=need, status=PledgeStatus.PLEDGED).exists()


@pytest.mark.django_db(transaction=True)
def test_two_completes_at_once_notify_each_donor_once():
    for _ in range(10):
        need = _need(needed=100, received=1)
        pledges = [_pledge(need) for _ in range(3)]
        bodies = []

        def call():
            res = _complete(need)
            bodies.append(res.json())
            return res

        codes = _race(2, call)
        assert codes == [200, 200]
        assert sorted(b["cancelled_pledges"] for b in bodies) == [0, 3]
        assert all(b["need_status"] == "closed" for b in bodies)
        for p in pledges:
            assert Notification.objects.filter(account=p.pledger_account,
                                               type="need_closed").count() == 1


@pytest.mark.django_db
def test_lowering_the_target_to_the_received_count_flips_to_fulfilled():
    need = _need(needed=20, received=8)
    res = _patch(need.shelter_account, need, {"quantity_needed": 8})
    assert res.status_code == 200 and res.json()["status"] == "fulfilled"
    need.refresh_from_db()
    assert need.status == NeedStatus.FULFILLED and need.fulfilled_at is not None
    assert need.quantity_needed == 8
    # and now it is no longer editable, like any fulfilled need
    assert _patch(need.shelter_account, need, {"title": "x"}).status_code == 409


@pytest.mark.django_db
def test_an_edit_that_does_not_meet_the_target_leaves_the_need_open():
    need = _need(needed=20, received=8)
    res = _patch(need.shelter_account, need, {"quantity_needed": 9, "title": "More"})
    assert res.status_code == 200 and res.json()["status"] == "open"
    need.refresh_from_db()
    assert need.status == NeedStatus.OPEN and need.fulfilled_at is None
    # a non-target edit of an already-at-target-by-received-zero need never flips either
    empty = _need(needed=5, received=0)
    assert _patch(empty.shelter_account, empty, {"title": "Y"}).json()["status"] == "open"


# --- Backlog · pledges stranded on needs closed before the refactor ----------------------------

@pytest.mark.django_db
def test_settle_stranded_pledges_cancels_only_pledged_pledges_on_closed_needs():
    """Before the refactor a close did not cascade, so a closed need could keep `pledged`
    pledges: the donor's card stayed "Pledged" with a live Cancel. The one-time data migration
    settles them the way a close settles them now, without a notification (those needs closed
    long ago)."""
    closed = _need(needed=10, received=4, status=NeedStatus.CLOSED)
    stranded = _pledge(closed)
    kept_delivered = _pledge(closed, status=PledgeStatus.DELIVERED)
    kept_cancelled = NeedPledge.objects.create(need=closed, pledger_account=AccountFactory(), quantity=1,
                                               status=PledgeStatus.CANCELLED, cancel_reason="donor")
    on_open = _pledge(_need(needed=10))
    on_fulfilled = _pledge(_need(needed=3, received=3, status=NeedStatus.FULFILLED))

    assert settle_stranded_pledges(NeedPledge, timezone.now()) == 1
    stranded.refresh_from_db()
    assert (stranded.status, stranded.cancel_reason) == ("cancelled", "need_closed")
    assert stranded.decided_at is not None
    for p, status, reason in [(kept_delivered, "delivered", None), (kept_cancelled, "cancelled", "donor"),
                              (on_open, "pledged", None), (on_fulfilled, "pledged", None)]:
        p.refresh_from_db()
        assert (p.status, p.cancel_reason) == (status, reason)
    assert Notification.objects.count() == 0
    assert settle_stranded_pledges(NeedPledge, timezone.now()) == 0      # idempotent


def test_the_migration_settles_stranded_pledges_with_the_historical_model():
    """The migration must hand the helper its historical model, and undoing it must not fail."""
    from importlib import import_module
    mig = import_module("community.migrations.0007_settle_stranded_pledges")
    ops = [op for op in mig.Migration.operations if op.__class__.__name__ == "RunPython"]
    assert len(ops) == 1 and ops[0].reverse_code is not None
    assert ("community", "0006_wishlist_refactor") in mig.Migration.dependencies
