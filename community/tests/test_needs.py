"""US-W1 · the Abot-tulong wishlist loop — needs & pledges.

D-S6-7: a pledge is a promise, not inventory. Only the shelter's received-confirm grows
`quantity_received`; the `open -> fulfilled` flip and that arithmetic live in one writer under
a row lock (proven by the concurrency test at the bottom).
"""
import threading

import pytest
from django.db import connection

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from community.models import NeedPledge, NeedStatus, PledgeStatus, ShelterNeed
from notifications.models import Notification


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter():
    return AccountFactory(account_type="shelter")


def _need(shelter=None, needed=5, received=0, status=NeedStatus.OPEN):
    return ShelterNeed.objects.create(shelter_account=shelter or _shelter(), title="Dog food",
                                      category="food", quantity_needed=needed,
                                      quantity_received=received, status=status)


def _pledge(need, pledger=None, quantity=1, status=PledgeStatus.PLEDGED):
    return NeedPledge.objects.create(need=need, pledger_account=pledger or AccountFactory(),
                                     quantity=quantity, status=status)


# --- posting & browsing needs -------------------------------------------------

@pytest.mark.django_db
def test_shelter_posts_a_need(client):
    sh = _shelter()
    res = client.post(f"/api/v1/shelters/{sh.pk}/needs",
                      {"title": "Rice", "category": "food", "quantity_needed": 10},
                      content_type="application/json", **_hdr(sh))
    assert res.status_code == 201
    need = ShelterNeed.objects.get(pk=res.json()["need_id"])
    assert need.shelter_account_id == sh.pk and need.status == NeedStatus.OPEN
    assert need.quantity_needed == 10 and need.quantity_received == 0


@pytest.mark.django_db
def test_a_non_shelter_cannot_post_a_need(client):
    sh = _shelter()
    giver = AccountFactory()
    res = client.post(f"/api/v1/shelters/{sh.pk}/needs",
                      {"title": "Rice", "category": "food", "quantity_needed": 1},
                      content_type="application/json", **_hdr(giver))
    assert res.status_code == 403


@pytest.mark.django_db
def test_a_shelter_cannot_post_to_another_shelters_needs(client):
    a, b = _shelter(), _shelter()
    res = client.post(f"/api/v1/shelters/{b.pk}/needs",
                      {"title": "Rice", "category": "food", "quantity_needed": 1},
                      content_type="application/json", **_hdr(a))
    assert res.status_code == 403


@pytest.mark.django_db
def test_needs_list_is_public(client):
    need = _need()
    res = client.get(f"/api/v1/shelters/{need.shelter_account_id}/needs")
    assert res.status_code == 200
    ids = [n["need_id"] for n in res.json()["results"]]
    assert str(need.pk) in ids


# --- pledging -----------------------------------------------------------------

@pytest.mark.django_db
def test_pledge_on_an_open_need(client):
    need = _need()
    giver = AccountFactory()
    res = client.post(f"/api/v1/needs/{need.pk}/pledges", {"quantity": 2},
                      content_type="application/json", **_hdr(giver))
    assert res.status_code == 201
    p = NeedPledge.objects.get(pk=res.json()["pledge_id"])
    assert p.status == PledgeStatus.PLEDGED and p.quantity == 2
    # D-S6-7: pledging does NOT touch quantity_received.
    need.refresh_from_db()
    assert need.quantity_received == 0
    # the shelter is told a pledge came in (D-S6-5)
    assert Notification.objects.filter(account=need.shelter_account,
                                       type="pledge_received").count() == 1


@pytest.mark.django_db
def test_pledge_on_a_fulfilled_need_is_409(client):
    need = _need(status=NeedStatus.FULFILLED)
    res = client.post(f"/api/v1/needs/{need.pk}/pledges", {"quantity": 1},
                      content_type="application/json", **_hdr(AccountFactory()))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "need_not_open"


@pytest.mark.django_db
def test_over_pledging_an_open_need_is_allowed(client):
    need = _need(needed=3)
    res = client.post(f"/api/v1/needs/{need.pk}/pledges", {"quantity": 10},
                      content_type="application/json", **_hdr(AccountFactory()))
    assert res.status_code == 201


# --- cancelling a pledge ------------------------------------------------------

@pytest.mark.django_db
def test_pledger_cancels_a_pledged_pledge(client):
    need = _need()
    giver = AccountFactory()
    p = _pledge(need, pledger=giver)
    res = client.post(f"/api/v1/pledges/{p.pk}/cancel", **_hdr(giver))
    assert res.status_code == 200
    p.refresh_from_db()
    assert p.status == PledgeStatus.CANCELLED


@pytest.mark.django_db
def test_cancelling_a_delivered_pledge_is_409(client):
    need = _need()
    giver = AccountFactory()
    p = _pledge(need, pledger=giver, status=PledgeStatus.DELIVERED)
    res = client.post(f"/api/v1/pledges/{p.pk}/cancel", **_hdr(giver))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "pledge_decided"


@pytest.mark.django_db
def test_only_the_pledger_can_cancel(client):
    need = _need()
    p = _pledge(need)
    res = client.post(f"/api/v1/pledges/{p.pk}/cancel", **_hdr(AccountFactory()))
    assert res.status_code in (403, 404)
    p.refresh_from_db()
    assert p.status == PledgeStatus.PLEDGED


# --- shelter received-confirm -------------------------------------------------

@pytest.mark.django_db
def test_received_confirm_marks_delivered_and_grows_received(client):
    need = _need(needed=5)
    p = _pledge(need, quantity=2)
    res = client.post(f"/api/v1/needs/{need.pk}/received",
                      {"pledge_id": str(p.pk), "quantity_received": 2},
                      content_type="application/json", **_hdr(need.shelter_account))
    assert res.status_code == 200
    p.refresh_from_db(); need.refresh_from_db()
    assert p.status == PledgeStatus.DELIVERED
    assert need.quantity_received == 2 and need.status == NeedStatus.OPEN
    # the pledger is told their pledge was confirmed received (D-S6-5)
    assert Notification.objects.filter(account=p.pledger_account,
                                       type="pledge_confirmed").count() == 1


@pytest.mark.django_db
def test_received_confirm_reaching_the_target_flips_to_fulfilled(client):
    need = _need(needed=3, received=1)
    p = _pledge(need, quantity=2)
    res = client.post(f"/api/v1/needs/{need.pk}/received",
                      {"pledge_id": str(p.pk), "quantity_received": 2},
                      content_type="application/json", **_hdr(need.shelter_account))
    assert res.status_code == 200
    assert res.json()["need_status"] == NeedStatus.FULFILLED
    need.refresh_from_db()
    assert need.quantity_received == 3 and need.status == NeedStatus.FULFILLED


@pytest.mark.django_db
def test_another_shelter_cannot_confirm_received(client):
    need = _need()
    p = _pledge(need)
    res = client.post(f"/api/v1/needs/{need.pk}/received",
                      {"pledge_id": str(p.pk), "quantity_received": 1},
                      content_type="application/json", **_hdr(_shelter()))
    assert res.status_code == 403


@pytest.mark.django_db
def test_shelter_edits_an_open_need(client):
    need = _need(needed=5)
    res = client.patch(f"/api/v1/needs/{need.pk}", {"title": "Kibble (large bag)",
                                                    "quantity_needed": 8},
                       content_type="application/json", **_hdr(need.shelter_account))
    assert res.status_code == 200
    need.refresh_from_db()
    assert need.title == "Kibble (large bag)" and need.quantity_needed == 8


@pytest.mark.django_db
def test_shelter_closes_a_need(client):
    need = _need()
    res = client.patch(f"/api/v1/needs/{need.pk}", {"status": "closed"},
                       content_type="application/json", **_hdr(need.shelter_account))
    assert res.status_code == 200
    need.refresh_from_db()
    assert need.status == NeedStatus.CLOSED


@pytest.mark.django_db
def test_editing_a_closed_need_is_409(client):
    need = _need(status=NeedStatus.CLOSED)
    res = client.patch(f"/api/v1/needs/{need.pk}", {"title": "x"},
                       content_type="application/json", **_hdr(need.shelter_account))
    assert res.status_code == 409 and res.json()["error"]["code"] == "need_not_open"


@pytest.mark.django_db
def test_another_shelter_cannot_edit_a_need(client):
    need = _need()
    res = client.patch(f"/api/v1/needs/{need.pk}", {"title": "x"},
                       content_type="application/json", **_hdr(_shelter()))
    assert res.status_code == 403


@pytest.mark.django_db
def test_shelter_lists_a_needs_pledges(client):
    need = _need()
    _pledge(need, quantity=2)
    _pledge(need, quantity=1)
    res = client.get(f"/api/v1/needs/{need.pk}/pledges", **_hdr(need.shelter_account))
    assert res.status_code == 200
    rows = res.json()["results"]
    assert len(rows) == 2 and all("pledger_name" in r for r in rows)


@pytest.mark.django_db
def test_another_shelter_cannot_list_pledges(client):
    need = _need()
    _pledge(need)
    res = client.get(f"/api/v1/needs/{need.pk}/pledges", **_hdr(_shelter()))
    assert res.status_code == 403


@pytest.mark.django_db
def test_my_pledges_lists_the_givers_own_pledges(client):
    giver = AccountFactory()
    mine = _pledge(_need(), pledger=giver, quantity=3)
    _pledge(_need())   # someone else's pledge
    res = client.get("/api/v1/me/pledges", **_hdr(giver))
    assert res.status_code == 200
    rows = res.json()["results"]
    assert len(rows) == 1 and rows[0]["pledge_id"] == str(mine.pk)
    assert rows[0]["need"]["shelter_name"] and rows[0]["quantity"] == 3


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_received_confirms_sum_correctly():
    """The lock is real: two staff confirming different pledges at once must both land,
    quantity_received summing to 4 — never lost-update to 2 (D-S6-7 / the US-V4 posture)."""
    from django.test import Client
    need = _need(needed=100)
    sh = need.shelter_account
    p1, p2 = _pledge(need, quantity=2), _pledge(need, quantity=2)
    barrier = threading.Barrier(2)

    def confirm(pledge_id):
        barrier.wait()
        Client().post(f"/api/v1/needs/{need.pk}/received",
                      {"pledge_id": str(pledge_id), "quantity_received": 2},
                      content_type="application/json", **_hdr(sh))
        connection.close()

    threads = [threading.Thread(target=confirm, args=(p.pk,)) for p in (p1, p2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    need.refresh_from_db()
    assert need.quantity_received == 4
