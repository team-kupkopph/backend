"""Abot-tulong wishlist · Task 1 — every pledge transition runs under the NEED row lock.

AW1: one pledge confirmed twice at once was counted twice (the status check read a pledge
loaded before the lock). AW5: cancel and create were unlocked check-then-write. AW6:
quantities had no upper bound, so a big number was a Postgres int overflow -> 500.
"""
import threading

import pytest
from django.db import connection, transaction
from django.test import Client

from accounts.factories import AccountFactory
from community.models import NeedPledge, NeedStatus, PledgeStatus, ShelterNeed
from community.needs import MAX_QTY, MAX_TOTAL
from community.tests.test_needs import _hdr, _need, _pledge, _shelter


def _post(acc, path, body=None):
    return Client().post(path, body or {}, content_type="application/json", **_hdr(acc))


def _received(need, pledge, qty):
    return _post(need.shelter_account, f"/api/v1/needs/{need.pk}/received",
                 {"pledge_id": str(pledge.pk), "quantity_received": qty})


def _race(n, fn):
    """Run `fn` on `n` threads released together by a barrier; return the status codes."""
    barrier = threading.Barrier(n)
    codes = []

    def run():
        barrier.wait()
        try:
            codes.append(fn().status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return codes


def _race_pair(a, b):
    """Run two different callables at once; return [a's status, b's status]."""
    barrier = threading.Barrier(2)
    out = [None, None]

    def run(i, fn):
        barrier.wait()
        try:
            out[i] = fn().status_code
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate((a, b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


# --- AW1 / AW5: no double count, no counted cancel ---------------------------------------

@pytest.mark.django_db(transaction=True)
def test_the_same_pledge_confirmed_twice_at_once_adds_once():
    need = _need(needed=100)
    p = _pledge(need, quantity=3)
    codes = _race(2, lambda: _received(need, p, 3))
    need.refresh_from_db()
    assert sorted(codes) == [200, 409] and need.quantity_received == 3


@pytest.mark.django_db(transaction=True)
def test_cancel_racing_receive_never_leaves_a_counted_cancelled_pledge():
    for _ in range(10):
        need = _need(needed=100)
        p = _pledge(need, quantity=2)
        _race_pair(lambda: _post(p.pledger_account, f"/api/v1/pledges/{p.pk}/cancel"),
                   lambda: _received(need, p, 2))
        p.refresh_from_db()
        need.refresh_from_db()
        assert (p.status, need.quantity_received) in {
            (PledgeStatus.CANCELLED, 0), (PledgeStatus.DELIVERED, 2)}


@pytest.mark.django_db(transaction=True)
def test_the_same_pledge_cancelled_twice_at_once_cancels_once():
    need = _need(needed=100)
    p = _pledge(need, quantity=2)
    codes = _race(2, lambda: _post(p.pledger_account, f"/api/v1/pledges/{p.pk}/cancel"))
    p.refresh_from_db()
    assert sorted(codes) == [200, 409] and p.status == PledgeStatus.CANCELLED


@pytest.mark.django_db(transaction=True)
def test_a_pledge_arriving_after_a_close_committed_is_refused():
    """Hold the need lock the way a closing writer does; a pledge that was already waiting
    on that lock must re-read the status once it gets it, not create a row on a closed need."""
    need = _need(needed=100)
    giver = AccountFactory()
    locked, release = threading.Event(), threading.Event()

    def closer():
        try:
            with transaction.atomic():
                ShelterNeed.objects.select_for_update().get(pk=need.pk)
                locked.set()
                release.wait(5)
                ShelterNeed.objects.filter(pk=need.pk).update(status=NeedStatus.CLOSED)
        finally:
            connection.close()

    t = threading.Thread(target=closer)
    t.start()
    assert locked.wait(5)
    result = []

    def pledger():
        try:
            result.append(_post(giver, f"/api/v1/needs/{need.pk}/pledges", {"quantity": 1}))
        finally:
            connection.close()

    p = threading.Thread(target=pledger)
    p.start()
    p.join(0.5)          # the pledge is parked on the row lock
    assert p.is_alive()
    release.set()
    t.join()
    p.join()
    assert result[0].status_code == 409
    assert result[0].json()["error"]["code"] == "need_not_open"
    assert not NeedPledge.objects.filter(need=need, status=PledgeStatus.PLEDGED).exists()


# --- receive: need status ----------------------------------------------------------------

@pytest.mark.django_db
def test_receive_on_a_fulfilled_need_still_counts_a_late_delivery():
    need = _need(needed=3, received=3, status=NeedStatus.FULFILLED)
    p = _pledge(need, quantity=2)
    res = _received(need, p, 2)
    assert res.status_code == 200 and res.json()["need_status"] == NeedStatus.FULFILLED
    p.refresh_from_db()
    need.refresh_from_db()
    assert p.status == PledgeStatus.DELIVERED
    assert need.quantity_received == 5 and need.status == NeedStatus.FULFILLED


@pytest.mark.django_db
def test_receive_on_a_closed_need_is_409():
    need = _need(status=NeedStatus.CLOSED)
    p = _pledge(need)
    res = _received(need, p, 1)
    assert res.status_code == 409 and res.json()["error"]["code"] == "need_not_open"
    p.refresh_from_db()
    need.refresh_from_db()
    assert p.status == PledgeStatus.PLEDGED and need.quantity_received == 0


@pytest.mark.django_db
def test_received_may_exceed_the_pledged_quantity():
    need = _need(needed=100)
    p = _pledge(need, quantity=2)
    res = _received(need, p, 5)
    assert res.status_code == 200
    need.refresh_from_db()
    assert need.quantity_received == 5


@pytest.mark.django_db
def test_receiving_a_pledge_of_another_need_is_404():
    need, other = _need(), _need()
    p = _pledge(other)
    res = _post(need.shelter_account, f"/api/v1/needs/{need.pk}/received",
                {"pledge_id": str(p.pk), "quantity_received": 1})
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"
    assert res.json()["error"]["message"] == "No such pledge for this need."


@pytest.mark.django_db
def test_receiving_a_decided_pledge_is_409_with_the_old_message():
    need = _need()
    p = _pledge(need, status=PledgeStatus.CANCELLED)
    res = _received(need, p, 1)
    assert res.status_code == 409 and res.json()["error"]["code"] == "pledge_decided"
    assert res.json()["error"]["message"] == "This pledge has already been decided."


# --- AW6: bounds -------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize("field,value", [("quantity", MAX_QTY + 1),
                                         ("quantity_received", 2**31),
                                         ("quantity_needed", MAX_QTY + 1)])
def test_quantities_above_max_are_400_not_500(field, value):
    sh = _shelter()
    if field == "quantity":
        res = _post(AccountFactory(), f"/api/v1/needs/{_need(sh).pk}/pledges", {field: value})
    elif field == "quantity_received":
        need = _need(sh)
        res = _post(sh, f"/api/v1/needs/{need.pk}/received",
                    {"pledge_id": str(_pledge(need).pk), field: value})
    else:
        res = _post(sh, f"/api/v1/shelters/{sh.pk}/needs",
                    {"title": "Rice", "category": "food", field: value})
    assert res.status_code == 400
    assert res.json()["error"]["field"] == field


@pytest.mark.django_db
def test_quantities_at_max_are_accepted():
    sh = _shelter()
    res = _post(sh, f"/api/v1/shelters/{sh.pk}/needs",
                {"title": "Rice", "category": "food", "quantity_needed": MAX_QTY})
    assert res.status_code == 201
    need = _need(sh, needed=MAX_QTY)
    assert _post(AccountFactory(), f"/api/v1/needs/{need.pk}/pledges",
                 {"quantity": MAX_QTY}).status_code == 201


@pytest.mark.django_db
def test_a_running_total_past_the_ceiling_is_422():
    need = _need(needed=100, received=MAX_TOTAL - 1)
    p = _pledge(need, quantity=5)
    res = _received(need, p, 5)
    assert res.status_code == 422 and res.json()["error"]["code"] == "total_overflow"
    p.refresh_from_db()
    need.refresh_from_db()
    assert p.status == PledgeStatus.PLEDGED and need.quantity_received == MAX_TOTAL - 1


@pytest.mark.django_db
def test_a_running_total_exactly_at_the_ceiling_is_allowed():
    need = _need(needed=100, received=MAX_TOTAL - 5)
    p = _pledge(need, quantity=5)
    assert _received(need, p, 5).status_code == 200
    need.refresh_from_db()
    assert need.quantity_received == MAX_TOTAL
