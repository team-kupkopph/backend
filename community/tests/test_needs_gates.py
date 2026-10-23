"""Task 2 · trust gates on the wishlist (AW2, AW7, AW31; decisions WD3, WD7).

Only an approved, active shelter may post a need; the public list and `create_pledge` see only
the needs of "live" shelters; a shelter may not pledge to its own need (other shelters may).
"""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from community.models import NeedPledge, ShelterNeed
from community.tests.test_needs import _hdr, _need, _shelter
from notifications.models import Notification

UNVERIFIED = "Your organization must be verified before posting wishlist needs."


def _post_need(client, shelter):
    return client.post(f"/api/v1/shelters/{shelter.pk}/needs",
                       {"title": "Rice", "category": "food", "quantity_needed": 10},
                       content_type="application/json", **_hdr(shelter))


def _pledge_to(client, need, who, quantity=1):
    return client.post(f"/api/v1/needs/{need.pk}/pledges", {"quantity": quantity},
                       content_type="application/json", **_hdr(who))


@pytest.mark.django_db
def test_an_unapproved_shelter_cannot_post_a_need(client):
    sh = _shelter(approved=False)
    res = _post_need(client, sh)
    assert res.status_code == 403
    assert res.json()["error"]["message"] == UNVERIFIED
    assert not ShelterNeed.objects.filter(shelter_account=sh).exists()


@pytest.mark.django_db
def test_an_unapproved_shelters_needs_are_not_listed(client):
    sh = _shelter(approved=False)
    _need(sh)
    res = client.get(f"/api/v1/shelters/{sh.pk}/needs")
    assert res.status_code == 200
    assert res.json() == {"results": []}


@pytest.mark.django_db
def test_the_owning_shelter_still_sees_its_own_needs_whatever_its_state(client):
    """R3: the gate hides a wishlist from the public, not from the shelter that owns it."""
    sh = _shelter(approved=False)
    need = _need(sh)
    mine = client.get(f"/api/v1/shelters/{sh.pk}/needs", **_hdr(sh))
    assert [n["need_id"] for n in mine.json()["results"]] == [str(need.pk)]
    # ...but another signed-in account, and a guest, get nothing.
    other = client.get(f"/api/v1/shelters/{sh.pk}/needs", **_hdr(AccountFactory()))
    assert other.json() == {"results": []}
    assert client.get(f"/api/v1/shelters/{sh.pk}/needs").json() == {"results": []}


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["suspended", "deleted"])
def test_an_inactive_shelters_needs_are_hidden_and_unpledgeable(client, status):
    fields = {"status": status}
    if status == "deleted":
        fields["deleted_at"] = timezone.now()      # the M5 CHECK ties the pair together
    sh = _shelter(**fields)
    need = _need(sh)
    giver = AccountFactory()

    listed = client.get(f"/api/v1/shelters/{sh.pk}/needs")
    assert listed.status_code == 200 and listed.json() == {"results": []}

    res = _pledge_to(client, need, giver)
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"
    assert not NeedPledge.objects.filter(need=need).exists()
    assert not Notification.objects.filter(account=sh).exists()


@pytest.mark.django_db
def test_a_shelter_cannot_pledge_to_its_own_need(client):
    sh = _shelter()
    need = _need(sh)
    res = _pledge_to(client, need, sh)
    assert res.status_code == 403
    err = res.json()["error"]
    assert err["code"] == "own_need"
    assert err["message"] == "A shelter can't pledge to its own wishlist."
    assert not NeedPledge.objects.filter(need=need).exists()


@pytest.mark.django_db
def test_an_unapproved_shelter_pledging_to_its_own_need_gets_own_need_not_404(client):
    sh = _shelter(approved=False)
    need = _need(sh)
    res = _pledge_to(client, need, sh)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "own_need"


@pytest.mark.django_db
def test_a_shelter_may_pledge_to_another_shelters_need(client):
    owner, other = _shelter(), _shelter()
    need = _need(owner)
    res = _pledge_to(client, need, other)
    assert res.status_code == 201
    assert NeedPledge.objects.filter(need=need, pledger_account=other).count() == 1
