"""AD20 · the claiming rescuer's report detail says where the handoff stands, so the case screen
can show 'Listed · live · 3 inquiries' instead of the rescuer finding out from a 409."""
import pytest
from django.contrib.gis.geos import Point

from listings.models import AdoptionListing, ListingStatus
from listings.tests.adoption_helpers import c, inquire, listing, person
from sagip.models import RescueCase, StrayReport, StrayStatus


def _case(rescuer, status=StrayStatus.SAFE):
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
                                        status=status, geom=Point(121.05, 14.63, srid=4326))
    return RescueCase.objects.create(report=report, claimed_by_account=rescuer), report


def _handoff(rescuer, report):
    return c(rescuer).get(f"/api/v1/reports/{report.pk}").json()["my_case"]["handoff"]


@pytest.mark.django_db
def test_no_handoff_yet_reads_null():
    rescuer = person(member=True)
    _, report = _case(rescuer)
    assert _handoff(rescuer, report) is None


@pytest.mark.django_db
def test_a_draft_a_live_listing_and_its_open_inquiries_are_reported():
    rescuer = person(member=True)
    _, report = _case(rescuer)
    item = listing(rescuer, source_report=report, status=ListingStatus.DRAFT)
    assert _handoff(rescuer, report) == {"listing_id": str(item.pk), "kind": "listing",
                                         "status": "draft", "open_inquiries": 0}
    AdoptionListing.objects.filter(pk=item.pk).update(status=ListingStatus.AVAILABLE)
    inquire(person(), item); inquire(person(), item)
    h = _handoff(rescuer, report)
    assert h["status"] == "available" and h["open_inquiries"] == 2


@pytest.mark.django_db
def test_a_placement_names_who_it_is_waiting_on():
    rescuer, recipient = person(member=True), person(member=True)
    case, report = _case(rescuer)
    placed = c(rescuer).post(f"/api/v1/cases/{case.pk}/place",
        {"recipient_email": recipient.email, "name": "Bruno", "adoption_fee": "0"}, format="json").json()
    h = _handoff(rescuer, report)
    assert h["kind"] == "placement" and h["status"] == "pending"
    assert h["listing_id"] == placed["listing_id"] and h["recipient"] == recipient.display_name


@pytest.mark.django_db
def test_a_withdrawn_listing_is_no_longer_the_handoff_and_only_the_claimer_sees_it():
    rescuer, other = person(member=True), person(member=True)
    _, report = _case(rescuer)
    listing(rescuer, source_report=report, status=ListingStatus.WITHDRAWN)
    assert _handoff(rescuer, report) is None
    assert "my_case" not in c(other).get(f"/api/v1/reports/{report.pk}").json()
