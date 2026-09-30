"""D7 (dev/sagip-build-review.md) · a shelter that accepts a placement gets the animal as a draft
listing in its own Animals tab, not a "pet" in its account.

Owner decision 2026-09-30. Before this, accepting created a `Pet` owned by the shelter — the
pet-owner concept — and the animal never appeared in the shelter's Animals tab (which lists the
shelter's own listings). Now: a private draft, posted by the shelter, carrying the species, the
name, the rescue it came from, the shelter's city and the photos (the placement's own, else the
stray report's). The shelter adds its story and fee and publishes it when ready. A Verified
Member recipient still adopts the animal as a Pet, exactly as before.
"""
import pytest
from django.contrib.gis.geos import Point
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from accounts.models import Address
from listings.models import AdoptionListing, AdoptionListingPhoto, Pet
from notifications.models import Notification
from sagip.models import RescueCase, StrayReport, StrayReportPhoto
from shelter.models import ShelterProfile
from verifications.models import AccountCapability, VerificationRequest


def _c(account=None):
    c = APIClient()
    if account is not None:
        c.force_authenticate(user=account)
    return c


def _member():
    a = AccountFactory()
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


def _shelter(city="Pasig City"):
    a = AccountFactory(account_type="shelter")
    VerificationRequest.objects.create(account=a, type="shelter_org", status="approved")
    ShelterProfile.objects.create(account=a, org_name="Paws Pasig", org_type="shelter",
                                  tier="registered_ngo")
    Address.objects.create(account=a, city=city, is_primary=True)
    return a


def _placed_with(recipient, report_photos=("s3://report-1.jpg", "s3://report-2.jpg")):
    rescuer = _member()
    report = StrayReport.objects.create(
        reporter_account=AccountFactory(), species="cat", condition="healthy", status="safe",
        city="Marikina", geom=Point(121.10, 14.65, srid=4326))
    for url in report_photos:
        StrayReportPhoto.objects.create(report=report, url=url)
    case = RescueCase.objects.create(report=report, claimed_by_account=rescuer)
    placed = _c(rescuer).post(f"/api/v1/cases/{case.pk}/place",
                              {"recipient_email": recipient.email, "name": "Mingming"},
                              format="json").json()
    return rescuer, report, placed


def _accept(recipient, placed):
    return _c(recipient).post(f"/api/v1/inquiries/{placed['inquiry_id']}/accept")


# ── the shelter gets a draft listing ─────────────────────────────────────────────────
@pytest.mark.django_db
def test_a_shelter_accepting_a_placement_gets_a_draft_listing_not_a_pet():
    shelter = _shelter()
    _, report, placed = _placed_with(shelter)

    res = _accept(shelter, placed)
    assert res.status_code == 200
    body = res.json()
    assert body["draft"] is True and "pet_id" not in body
    assert not Pet.objects.filter(owner_account=shelter).exists()

    draft = AdoptionListing.objects.get(pk=body["listing_id"])
    assert draft.posted_by_id == shelter.pk and draft.status == "draft"
    assert (draft.species, draft.name) == ("cat", "Mingming")
    assert draft.source_report_id == report.pk        # provenance: which rescue it came from
    assert draft.city == "Pasig City"                 # where the animal is now, not where found
    assert draft.adoption_fee == 0 and draft.story == ""


@pytest.mark.django_db
def test_the_draft_carries_the_rescue_photos_first_one_primary():
    shelter = _shelter()
    _, _, placed = _placed_with(shelter)
    draft_id = _accept(shelter, placed).json()["listing_id"]
    photos = list(AdoptionListingPhoto.objects.filter(listing_id=draft_id).order_by("-is_primary", "url"))
    assert [p.url for p in photos] == ["s3://report-1.jpg", "s3://report-2.jpg"]
    assert photos[0].is_primary and not photos[1].is_primary


@pytest.mark.django_db
def test_the_placement_listing_itself_is_closed_as_placed_with_the_shelter():
    shelter = _shelter()
    _, _, placed = _placed_with(shelter)
    _accept(shelter, placed)
    listing = AdoptionListing.objects.get(pk=placed["listing_id"])
    assert listing.status == "adopted"
    assert listing.adopted_by_account_id == shelter.pk and listing.adopted_pet_id is None


@pytest.mark.django_db
def test_the_rescue_still_closes_and_everyone_is_told():
    shelter = _shelter()
    rescuer, report, placed = _placed_with(shelter)
    _accept(shelter, placed)
    report.refresh_from_db()
    assert report.status == "resolved"
    assert Notification.objects.filter(account=rescuer, type="placement_decided",
                                       data__decision="accepted").exists()


@pytest.mark.django_db
def test_a_member_recipient_still_adopts_the_animal_as_a_pet():
    member = _member()
    _, _, placed = _placed_with(member)
    body = _accept(member, placed).json()
    assert "pet_id" in body and "draft" not in body
    assert Pet.objects.filter(owner_account=member).count() == 1
    assert not AdoptionListing.objects.filter(posted_by=member).exists()


# ── a draft stays private until the shelter publishes it ─────────────────────────────
def _draft_for(shelter):
    _, _, placed = _placed_with(shelter)
    return AdoptionListing.objects.get(pk=_accept(shelter, placed).json()["listing_id"])


@pytest.mark.django_db
def test_a_draft_is_in_the_shelters_own_list_and_nowhere_public():
    shelter = _shelter()
    draft = _draft_for(shelter)
    mine = _c(shelter).get("/api/v1/listings?mine=true&status=draft").json()["results"]
    assert [r["listing_id"] for r in mine] == [str(draft.pk)]
    public = _c().get("/api/v1/listings").json()["results"]
    assert str(draft.pk) not in [r["listing_id"] for r in public]


@pytest.mark.django_db
def test_only_its_poster_can_open_a_draft():
    shelter = _shelter()
    draft = _draft_for(shelter)
    assert _c(shelter).get(f"/api/v1/listings/{draft.pk}").json()["status"] == "draft"
    assert _c(AccountFactory()).get(f"/api/v1/listings/{draft.pk}").status_code == 404
    assert _c().get(f"/api/v1/listings/{draft.pk}").status_code == 404


@pytest.mark.django_db
def test_no_one_can_inquire_on_a_draft():
    shelter = _shelter()
    draft = _draft_for(shelter)
    adopter = _member()
    from django.utils import timezone
    adopter.phone, adopter.phone_verified_at = "+639170000001", timezone.now()
    adopter.save(update_fields=["phone", "phone_verified_at"])
    res = _c(adopter).post(f"/api/v1/listings/{draft.pk}/inquiries", {}, format="json")
    assert res.status_code == 404


@pytest.mark.django_db
def test_the_shelter_publishes_a_draft_onto_the_adopt_feed():
    shelter = _shelter()
    draft = _draft_for(shelter)
    res = _c(shelter).post(f"/api/v1/listings/{draft.pk}/publish")
    assert res.status_code == 200 and res.json()["status"] == "available"
    public = _c().get("/api/v1/listings").json()["results"]
    assert str(draft.pk) in [r["listing_id"] for r in public]


@pytest.mark.django_db
def test_only_the_poster_can_publish_and_only_a_draft():
    shelter = _shelter()
    draft = _draft_for(shelter)
    assert _c(AccountFactory()).post(f"/api/v1/listings/{draft.pk}/publish").status_code == 403
    _c(shelter).post(f"/api/v1/listings/{draft.pk}/publish")
    res = _c(shelter).post(f"/api/v1/listings/{draft.pk}/publish")
    assert res.status_code == 409 and res.json()["error"]["code"] == "not_draft"
