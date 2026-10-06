"""AD6 · the poster can turn an applicant down, the adopter can withdraw, and every way an
inquiry ends records who ended it and why."""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from listings.models import AdoptionInquiry, AdoptionListing, ListingStatus
from listings.sweeps import PLACEMENT_EXPIRY_DAYS, expire_placements
from listings.tests.adoption_helpers import c, inquire, listing, person, post, shelter
from sagip.models import RescueCase, StrayReport, StrayStatus


@pytest.mark.django_db
def test_the_poster_rejects_with_a_reason_and_the_adopter_hears_it():
    poster, adopter = shelter(), person()
    inq = inquire(adopter, listing(poster, name="Milo"))
    res = post(poster, inq, "reject", {"reason": "requirements_not_met"})
    assert res.status_code == 200 and res.json() == {"status": "declined",
                                                     "end_reason": "requirements_not_met"}
    inq.refresh_from_db()
    assert inq.ended_by_account_id == poster.pk and inq.decided_at is not None
    n = adopter.notifications.get(type="inquiry_rejected")
    assert n.title == "About your inquiry"
    assert n.body == ("Paws Marikina won't be going ahead with your inquiry about Milo: "
                      "the adoption requirements aren't met.")


@pytest.mark.django_db
def test_reject_needs_one_of_the_four_reasons_and_the_poster():
    poster, adopter = shelter(), person()
    inq = inquire(adopter, listing(poster))
    bad = post(poster, inq, "reject", {"reason": "another_adopter_chosen"})   # system-only reason
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_reason"
    assert post(adopter, inq, "reject", {"reason": "other"}).status_code == 403
    assert post(poster, inq, "reject", {"reason": "other"}).status_code == 200
    again = post(poster, inq, "reject", {"reason": "other"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "inquiry_closed"


@pytest.mark.django_db
def test_the_adopter_withdraws_and_the_poster_hears_it():
    poster, adopter = shelter(), person()
    inq = inquire(adopter, listing(poster, name="Milo"))
    assert post(person(), inq, "withdraw").json()["error"]["code"] == "not_your_inquiry"
    res = post(adopter, inq, "withdraw")
    assert res.status_code == 200 and res.json()["status"] == "withdrawn"
    inq.refresh_from_db()
    assert inq.end_reason == "adopter_withdrew" and inq.ended_by_account_id == adopter.pk
    n = poster.notifications.get(type="inquiry_withdrawn")
    assert n.body == f"{adopter.display_name} is no longer applying for Milo."
    assert n.data["poster_is_shelter"] is True


@pytest.mark.django_db
def test_ending_the_reserved_applicant_reopens_the_listing():
    # Built through the ORM because Reserve arrives in Task 6.
    poster, adopter = shelter(), person(member=True)
    the_listing = listing(poster, name="Milo")
    inq = inquire(adopter, the_listing)
    AdoptionInquiry.objects.filter(pk=inq.pk).update(accepted_at=timezone.now(),
                                                     reserved_at=timezone.now())
    AdoptionListing.objects.filter(pk=the_listing.pk).update(status=ListingStatus.PENDING)
    assert post(adopter, inq, "withdraw").status_code == 200
    the_listing.refresh_from_db(); inq.refresh_from_db()
    assert the_listing.status == ListingStatus.AVAILABLE and inq.reserved_at is None
    n = poster.notifications.get(type="inquiry_withdrawn")
    assert n.body.endswith("Milo is back on the Adopt feed.")


@pytest.mark.django_db
def test_a_placement_recipient_declines_with_decline_not_withdraw():
    rescuer, recipient = person(member=True), person(member=True)
    inq = AdoptionInquiry.objects.create(listing=listing(rescuer, status="pending"),
                                         adopter_account=recipient, kind="placement")
    res = post(recipient, inq, "withdraw")
    assert res.status_code == 409 and res.json()["error"]["code"] == "is_placement"


# ── the existing ending paths now record who and why ─────────────────────────────────────────
def _safe_case(rescuer):
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
        status=StrayStatus.SAFE, geom=Point(121.05, 14.63, srid=4326))
    return RescueCase.objects.create(report=report, claimed_by_account=rescuer)


def _place(rescuer, recipient):
    case = _safe_case(rescuer)
    res = c(rescuer).post(f"/api/v1/cases/{case.pk}/place",
                          {"recipient_email": recipient.email, "name": "Bruno"}, format="json")
    assert res.status_code == 201, res.content
    return case, AdoptionInquiry.objects.get(pk=res.json()["inquiry_id"])


@pytest.mark.django_db
def test_placement_endings_record_who_and_why():
    rescuer = person(member=True)
    case, cancelled = _place(rescuer, person(member=True))
    c(rescuer).post(f"/api/v1/cases/{case.pk}/handoff/cancel", {}, format="json")
    cancelled.refresh_from_db()
    assert (cancelled.end_reason, cancelled.ended_by_account_id) == ("placement_cancelled", rescuer.pk)

    recipient = person(member=True)
    _, declined = _place(rescuer, recipient)
    post(recipient, declined, "decline")
    declined.refresh_from_db()
    assert (declined.end_reason, declined.ended_by_account_id) == ("placement_declined", recipient.pk)

    _, lapsed = _place(rescuer, person(member=True))
    expire_placements(now=timezone.now() + timezone.timedelta(days=PLACEMENT_EXPIRY_DAYS + 1))
    lapsed.refresh_from_db()
    assert (lapsed.end_reason, lapsed.ended_by_account_id) == ("placement_expired", None)


@pytest.mark.django_db
def test_a_d15_take_back_records_listing_withdrawn_by_the_rescuer():
    rescuer = person(member=True); case = _safe_case(rescuer)
    lid = c(rescuer).post(f"/api/v1/cases/{case.pk}/list", {}, format="json").json()["listing_id"]
    AdoptionListing.objects.filter(pk=lid).update(status=ListingStatus.AVAILABLE)
    inq = inquire(person(), AdoptionListing.objects.get(pk=lid))
    c(rescuer).post(f"/api/v1/cases/{case.pk}/handoff/cancel", {"close_inquiries": True}, format="json")
    inq.refresh_from_db()
    assert (inq.end_reason, inq.ended_by_account_id) == ("listing_withdrawn", rescuer.pk)
