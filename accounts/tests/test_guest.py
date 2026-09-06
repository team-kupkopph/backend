import pytest


@pytest.mark.django_db
def test_gated_write_without_token_is_auth_required_401(client):
    res = client.post("/api/v1/me/location", {"city": "X"}, content_type="application/json")
    assert res.status_code == 401
    assert res.json()["error"]["code"] in ("auth_required", "not_authenticated")


@pytest.mark.django_db
def test_listings_public_returns_only_verified_poster_rows(client):
    from accounts.factories import AccountFactory
    from listings.models import AdoptionListing
    from verifications.models import AccountCapability
    verified = AccountFactory(email="v@ex.com")
    AccountCapability.objects.create(account=verified, capability="rescuer", status="approved")
    unverified = AccountFactory(email="u@ex.com")
    AdoptionListing.objects.create(posted_by=verified, name="Milo", species="dog",
                                   city="Marikina", status="available")
    AdoptionListing.objects.create(posted_by=unverified, name="Hidden", species="cat",
                                   city="Marikina", status="available")
    res = client.get("/api/v1/listings?city=Marikina")
    names = [r["pet"]["name"] for r in res.json()["results"]]
    assert names == ["Milo"]     # unverified poster's row is invisible (decision 3 predicate)
