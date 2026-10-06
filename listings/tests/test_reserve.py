"""AQ4 · the poster reserves the animal for one screened applicant; the listing reads Reserved
and takes no new inquiries; the others keep waiting. AQ2 · Reserve needs the adopter's badge."""
import pytest
from django.contrib.gis.geos import Point

from listings.models import AdoptionInquiry, AdoptionListing, AdoptionStage, ListingStatus
from listings.tests.adoption_helpers import c, inquire, listing, person, post, shelter, types
from sagip.models import RescueCase, StrayReport, StrayStatus


def _screened(poster, the_listing, member=True):
    adopter = person(member=member)
    inq = inquire(adopter, the_listing)
    assert post(poster, inq, "screen").status_code == 200
    return adopter, inq


def _safe_case(rescuer):
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
        status=StrayStatus.SAFE, geom=Point(121.05, 14.63, srid=4326))
    return RescueCase.objects.create(report=report, claimed_by_account=rescuer)


@pytest.mark.django_db
def test_reserve_holds_the_animal_and_others_keep_waiting():
    poster = shelter(); the_listing = listing(poster, name="Milo")
    adopter, inq = _screened(poster, the_listing)
    other, other_inq = _screened(poster, the_listing)
    res = post(poster, inq, "reserve")
    assert res.status_code == 200 and res.json()["reserved_at"] is not None
    the_listing.refresh_from_db()
    assert the_listing.status == ListingStatus.PENDING
    assert AdoptionStage.objects.get(inquiry=inq, stage_key="finalization").state == "in_progress"
    assert AdoptionInquiry.objects.get(pk=other_inq.pk).status == "active"     # still waiting
    assert adopter.notifications.get(type="adoption_reserved").title == "Milo is reserved for you"
    late = c(person()).post(f"/api/v1/listings/{the_listing.pk}/inquiries", {}, format="json")
    assert late.status_code == 409 and late.json()["error"]["code"] == "listing_unavailable"
    taken = post(poster, other_inq, "reserve")
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "reserved_for_another"
    again = post(poster, inq, "reserve")
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_reserved"


@pytest.mark.django_db
def test_aq2_reserve_needs_the_adopters_badge_and_tells_them_what_is_missing():
    poster = shelter(); the_listing = listing(poster, name="Milo")
    adopter, inq = _screened(poster, the_listing, member=False)
    res = post(poster, inq, "reserve")
    assert res.status_code == 409 and res.json()["error"]["code"] == "adopter_badge_required"
    the_listing.refresh_from_db()
    assert the_listing.status == ListingStatus.AVAILABLE
    assert AdoptionInquiry.objects.get(pk=inq.pk).reserved_at is None
    n = adopter.notifications.get(type="adoption_badge_needed")
    assert n.title == "One step before you can adopt Milo"


@pytest.mark.django_db
def test_reserve_needs_screening_first():
    poster = shelter(); the_listing = listing(poster)
    inq = inquire(person(member=True), the_listing)
    res = post(poster, inq, "reserve")
    assert res.status_code == 409 and res.json()["error"]["code"] == "not_screening"


@pytest.mark.django_db
def test_unreserve_reopens_the_listing_and_keeps_the_applicant():
    poster = shelter(); the_listing = listing(poster, name="Milo")
    adopter, inq = _screened(poster, the_listing)
    post(poster, inq, "reserve")
    res = post(poster, inq, "unreserve")
    assert res.status_code == 200 and res.json()["reserved_at"] is None and res.json()["status"] == "active"
    the_listing.refresh_from_db()
    assert the_listing.status == ListingStatus.AVAILABLE
    assert AdoptionStage.objects.get(inquiry=inq, stage_key="finalization").state == "not_started"
    assert types(adopter)[-1] == "reservation_released"
    again = post(poster, inq, "unreserve")
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_reserved"


@pytest.mark.django_db
def test_a_d15_take_back_clears_the_reservation_on_the_withdrawn_applicant():
    rescuer = person(member=True); case = _safe_case(rescuer)
    lid = c(rescuer).post(f"/api/v1/cases/{case.pk}/list", {}, format="json").json()["listing_id"]
    AdoptionListing.objects.filter(pk=lid).update(status=ListingStatus.AVAILABLE)
    the_listing = AdoptionListing.objects.get(pk=lid)
    _, inq = _screened(rescuer, the_listing)
    assert post(rescuer, inq, "reserve").status_code == 200
    res = c(rescuer).post(f"/api/v1/cases/{case.pk}/handoff/cancel", {"close_inquiries": True},
                          format="json")
    assert res.status_code == 200, res.content
    inq.refresh_from_db(); the_listing.refresh_from_db()
    assert inq.status == "withdrawn" and inq.reserved_at is None
    assert the_listing.status == ListingStatus.WITHDRAWN
