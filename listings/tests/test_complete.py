"""AD5 / AQ3 · Complete finishes a public adoption in one transaction: inquiry and listing
adopted, the adopter's Pet, every other applicant closed and told, a rescue report resolved,
badges counted."""
import pytest
from django.contrib.gis.geos import Point

from listings.models import AdoptionListingPhoto, AdoptionStage, ListingStatus, Pet
from listings.tests.adoption_helpers import c, inquire, listing, person, post, shelter, types
from notifications.models import Notification
from sagip.models import RescueCase, StrayReport, StrayStatus
from verifications.models import AccountCapability


def _reserved(poster, the_listing):
    adopter = person(member=True)
    inq = inquire(adopter, the_listing)
    assert post(poster, inq, "screen").status_code == 200
    assert post(poster, inq, "reserve").status_code == 200
    return adopter, inq


@pytest.mark.django_db
def test_complete_finishes_the_adoption():
    poster = shelter(); the_listing = listing(poster, name="Milo")
    AdoptionListingPhoto.objects.create(listing=the_listing, url="https://x/milo.jpg", is_primary=True)
    waiting = inquire(person(), the_listing)
    adopter, inq = _reserved(poster, the_listing)
    res = post(poster, inq, "complete")
    assert res.status_code == 200
    pet = Pet.objects.get(pk=res.json()["pet_id"])
    assert pet.owner_account_id == adopter.pk and pet.name == "Milo"
    assert [p.url for p in pet.photos.all()] == ["https://x/milo.jpg"]
    inq.refresh_from_db(); the_listing.refresh_from_db(); waiting.refresh_from_db()
    assert inq.status == "adopted" and inq.decided_at is not None
    assert the_listing.status == ListingStatus.ADOPTED
    assert the_listing.adopted_by_account_id == adopter.pk and the_listing.adopted_pet_id == pet.pk
    assert AdoptionStage.objects.get(inquiry=inq, stage_key="finalization").state == "done"
    assert (waiting.status, waiting.end_reason) == ("declined", "another_adopter_chosen")
    assert Notification.objects.get(account=waiting.adopter_account, type="inquiry_rejected").title \
        == "Milo found a home"
    done = adopter.notifications.get(type="adoption_completed")
    assert done.title == "Welcome home, Milo!" and done.data["pet_id"] == str(pet.pk)
    # The adopter keeps the poster's number after the adoption (AQ1).
    assert "poster_contact" in c(adopter).get(f"/api/v1/inquiries/{inq.pk}").json()


@pytest.mark.django_db
def test_complete_needs_a_reservation_and_a_badge_still_held():
    poster = shelter(); the_listing = listing(poster)
    adopter = person(member=True); inq = inquire(adopter, the_listing)
    post(poster, inq, "screen")
    early = post(poster, inq, "complete")
    assert early.status_code == 409 and early.json()["error"]["code"] == "not_reserved"
    post(poster, inq, "reserve")
    AccountCapability.objects.filter(account=adopter).update(status="rejected")
    revoked = post(poster, inq, "complete")
    assert revoked.status_code == 409 and revoked.json()["error"]["code"] == "adopter_badge_required"
    assert not Pet.objects.exists()
    assert post(person(), inq, "complete").status_code == 403


@pytest.mark.django_db
def test_completing_a_rescue_listing_resolves_the_report_and_tells_the_reporter():
    """The D4 remainder: an adopted rescue listing resolves its report."""
    rescuer, reporter = person(member=True), person()
    report = StrayReport.objects.create(reporter_account=reporter, species="dog", condition="injured",
        city="Marikina", status=StrayStatus.SAFE, geom=Point(121.05, 14.63, srid=4326))
    RescueCase.objects.create(report=report, claimed_by_account=rescuer)
    the_listing = listing(rescuer, name="Bruno", source_report=report)
    adopter, inq = _reserved(rescuer, the_listing)
    assert post(rescuer, inq, "complete").status_code == 200
    report.refresh_from_db()
    assert report.status == StrayStatus.RESOLVED
    assert RescueCase.objects.get(report=report).resolved_at is not None
    assert "case_progress" in types(reporter)
