import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from listings.models import AdoptionListing
from verifications.models import VerificationRequest


@pytest.mark.django_db
def test_reports_map_is_public_and_returns_contract_shape(client):
    # No city given: the map can't search anywhere, and now says so (S15).
    res = client.get("/api/v1/reports/map")
    assert res.status_code == 200 and res.json() == {"reports": [], "city_supported": False}


@pytest.mark.django_db
def test_listings_show_a_verified_shelters_listing(client):
    # US-B5 visibility helper: a verified shelter's listing is public.
    shelter = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    VerificationRequest.objects.create(account=shelter, type="shelter_org", status="approved")
    AdoptionListing.objects.create(posted_by=shelter, name="Bantay", species="dog", city="Marikina")
    res = client.get("/api/v1/listings?city=Marikina")
    assert res.status_code == 200
    names = [r["pet"]["name"] for r in res.json()["results"]]
    assert "Bantay" in names


@pytest.mark.django_db
def test_listings_hide_an_unverified_shelters_listing(client):
    shelter = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    VerificationRequest.objects.create(account=shelter, type="shelter_org", status="pending")
    AdoptionListing.objects.create(posted_by=shelter, name="Hidden", species="cat", city="Marikina")
    res = client.get("/api/v1/listings?city=Marikina")
    assert res.json()["results"] == []
