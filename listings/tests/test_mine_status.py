import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from listings.models import AdoptionListing


def _hdr(acc): return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


@pytest.mark.django_db
@pytest.mark.parametrize("status,expected", [("available", 2), ("pending", 1), ("adopted", 1)])
def test_mine_filters_by_status(client, status, expected):
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    for s in ["available", "available", "pending", "adopted"]:
        AdoptionListing.objects.create(posted_by=acc, species="dog", name=s, city="Marikina", status=s)
    res = client.get(f"/api/v1/listings?mine=true&status={status}", **_hdr(acc))
    assert res.status_code == 200 and len(res.json()["results"]) == expected


@pytest.mark.django_db
def test_mine_default_is_still_available_only(client):
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    AdoptionListing.objects.create(posted_by=acc, species="dog", name="a", city="Marikina", status="adopted")
    assert client.get("/api/v1/listings?mine=true", **_hdr(acc)).json()["results"] == []


@pytest.mark.django_db
def test_unknown_status_is_422(client):
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    assert client.get("/api/v1/listings?mine=true&status=lost", **_hdr(acc)).status_code == 422
