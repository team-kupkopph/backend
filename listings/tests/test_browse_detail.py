"""US-A3 — GET /listings (extended: full card data + pagination) and
GET /listings/{id} (public detail)."""
import uuid

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from listings.models import AdoptionListing, AdoptionListingPhoto
from verifications.models import AccountCapability


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _verified_member(**kw):
    acc = AccountFactory(**kw)
    AccountCapability.objects.create(account=acc, capability="rescuer", status="approved",
                                     granted_at=timezone.now())
    return acc


def _listing(poster, **kw):
    defaults = dict(name="Bantay", species="dog", city="Marikina", adoption_fee="300.00")
    defaults.update(kw)
    return AdoptionListing.objects.create(posted_by=poster, **defaults)


# ── GET /listings — card data ────────────────────────────────────────────────────
@pytest.mark.django_db
def test_list_card_includes_pet_fields_photo_and_fee(client):
    member = _verified_member()
    listing = _listing(member, breed="Aspin", sex="male", walkable=True)
    AdoptionListingPhoto.objects.create(listing=listing, url="https://example.invalid/primary",
                                        is_primary=True)
    AdoptionListingPhoto.objects.create(listing=listing, url="https://example.invalid/other")

    body = client.get("/api/v1/listings?city=Marikina").json()
    card = body["results"][0]
    assert card["pet"]["breed"] == "Aspin" and card["pet"]["sex"] == "male"
    assert card["pet"]["walkable"] is True
    assert card["adoption_fee"] == "300.00"
    assert card["photo_url"] == "https://example.invalid/primary"  # primary wins
    # The card names its poster, the same shape the detail returns, so the Adopt deck can show
    # who is offering the animal without a detail fetch per card.
    assert card["poster"]["account_id"] == str(member.pk)
    assert card["poster"]["is_shelter"] is False
    assert card["poster"]["name"] == member.display_name


@pytest.mark.django_db
def test_species_filter(client):
    member = _verified_member()
    _listing(member, name="Bantay", species="dog", city="Marikina")
    _listing(member, name="Muning", species="cat", city="Marikina")
    body = client.get("/api/v1/listings?city=Marikina&species=cat").json()
    assert [r["pet"]["name"] for r in body["results"]] == ["Muning"]


@pytest.mark.django_db
def test_still_hides_an_unverified_posters_listing(client):
    unverified = AccountFactory()
    _listing(unverified, city="Marikina")
    assert client.get("/api/v1/listings?city=Marikina").json()["results"] == []


# ── Pagination ────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_pagination_returns_a_next_page_number_when_more_remain(client):
    member = _verified_member()
    for i in range(25):
        _listing(member, name=f"Pet{i}", city="Marikina")

    page1 = client.get("/api/v1/listings?city=Marikina").json()
    assert len(page1["results"]) == 20
    assert page1["next"] == 2

    page2 = client.get("/api/v1/listings?city=Marikina&page=2").json()
    assert len(page2["results"]) == 5
    assert page2["next"] is None
    # no overlap between pages
    ids1 = {r["listing_id"] for r in page1["results"]}
    ids2 = {r["listing_id"] for r in page2["results"]}
    assert ids1.isdisjoint(ids2)


# ── mine=true — owner filter for the shelter animal picker (US-V9) ─────────────────
@pytest.mark.django_db
def test_mine_true_returns_only_own_available_listings(client):
    """Ownership is the gate for mine=true — public_poster_q() is skipped, so an
    unverified poster's own listing still shows in their own picker even though it
    would be hidden from the public browse feed."""
    me = AccountFactory(account_type="shelter")
    other = AccountFactory(account_type="shelter")
    mine = _listing(me, name="Rex", city="Manila")
    _listing(other, name="Bo", city="Manila")

    body = client.get("/api/v1/listings?mine=true", **_hdr(me)).json()
    assert [r["listing_id"] for r in body["results"]] == [str(mine.listing_id)]


@pytest.mark.django_db
def test_mine_true_requires_auth(client):
    res = client.get("/api/v1/listings?mine=true")
    assert res.status_code == 401


# ── GET /listings/{id} — public detail ───────────────────────────────────────────
@pytest.mark.django_db
def test_detail_is_public_and_full(client):
    member = _verified_member(display_name="Ana Cruz")
    listing = _listing(member, name="Bantay", breed="Aspin", requirements="Fenced yard preferred")
    AdoptionListingPhoto.objects.create(listing=listing, url="https://example.invalid/1")

    body = client.get(f"/api/v1/listings/{listing.pk}").json()
    assert body["pet"]["name"] == "Bantay" and body["pet"]["breed"] == "Aspin"
    assert body["requirements"] == "Fenced yard preferred"
    assert body["photos"] == ["https://example.invalid/1"]
    assert body["poster"]["name"] == "Ana Cruz" and body["poster"]["is_shelter"] is False


@pytest.mark.django_db
def test_detail_shows_the_org_name_for_a_shelter_poster(client):
    from shelter.models import ShelterProfile
    from verifications.models import VerificationRequest
    shelter = AccountFactory(account_type="shelter")
    VerificationRequest.objects.create(account=shelter, type="shelter_org", status="approved")
    ShelterProfile.objects.create(account=shelter, org_name="Marikina AWG", org_type="shelter",
                                  tier="community_rescue")
    listing = _listing(shelter, name="Luna")

    body = client.get(f"/api/v1/listings/{listing.pk}").json()
    assert body["poster"]["name"] == "Marikina AWG" and body["poster"]["is_shelter"] is True
    # US-Q2 · the client needs this to link to GET /shelters/{account_id}/donation-qr.
    assert body["poster"]["account_id"] == str(shelter.pk)


@pytest.mark.django_db
def test_detail_404_for_unknown_listing(client):
    assert client.get(f"/api/v1/listings/{uuid.uuid4()}").status_code == 404


@pytest.mark.django_db
def test_detail_is_visible_even_for_a_listing_by_an_unverified_poster(client):
    """Detail is a direct-link page (like report-detail) — it doesn't re-apply the
    browse-feed visibility gate, only the list does."""
    unverified = AccountFactory()
    listing = _listing(unverified)
    res = client.get(f"/api/v1/listings/{listing.pk}")
    assert res.status_code == 200
