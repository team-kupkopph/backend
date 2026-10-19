"""AD12 (every listing starts as a draft; publishing needs a verified poster) and AD10 (any poster
can withdraw a listing or mark it adopted elsewhere)."""
import pytest

from listings.models import AdoptionListing, EndReason, InquiryStatus, ListingStatus
from listings.tests.adoption_helpers import c, inquire, listing, person, post, shelter, types

BODY = {"pet": {"name": "Bantay", "species": "dog"}, "adoption_fee": "0", "city": "Marikina"}


@pytest.mark.django_db
def test_a_new_listing_is_born_a_draft_and_is_not_public():
    poster, other = person(member=True), person()
    res = c(poster).post("/api/v1/listings", BODY, format="json")
    assert res.status_code == 201 and res.json()["listing_status"] == "draft"
    lid = res.json()["listing_id"]
    assert c(other).get("/api/v1/listings").json()["results"] == []
    assert c(other).get(f"/api/v1/listings/{lid}").status_code == 404


@pytest.mark.django_db
def test_publish_puts_a_verified_posters_draft_on_the_feed():
    poster, other = person(member=True), person()
    lid = c(poster).post("/api/v1/listings", BODY, format="json").json()["listing_id"]
    res = c(poster).post(f"/api/v1/listings/{lid}/publish")
    assert res.status_code == 200 and res.json()["status"] == "available"
    assert [r["listing_id"] for r in c(other).get("/api/v1/listings").json()["results"]] == [lid]


@pytest.mark.django_db
def test_publish_is_refused_to_an_unverified_poster_instead_of_going_live_invisibly():
    poster = person(member=False)
    lid = c(poster).post("/api/v1/listings", BODY, format="json").json()["listing_id"]
    res = c(poster).post(f"/api/v1/listings/{lid}/publish")
    assert res.status_code == 403 and res.json()["error"]["code"] == "verification_required"
    assert AdoptionListing.objects.get(pk=lid).status == ListingStatus.DRAFT


@pytest.mark.django_db
def test_an_unverified_shelter_cannot_publish_a_d7_draft_either():
    unverified = person()
    unverified.account_type = "shelter"; unverified.save()
    draft = listing(unverified, verify=False, status=ListingStatus.DRAFT)
    assert c(unverified).post(f"/api/v1/listings/{draft.pk}/publish").status_code == 403


# ── AD10 · withdraw ─────────────────────────────────────────────────────────────
def _withdraw(who, the_listing, body=None):
    return c(who).post(f"/api/v1/listings/{the_listing.pk}/withdraw", body or {}, format="json")


@pytest.mark.django_db
def test_a_poster_can_withdraw_a_draft_or_a_listing_nobody_asked_about():
    poster = person()
    for status in (ListingStatus.DRAFT, ListingStatus.AVAILABLE):
        item = listing(poster, status=status)
        res = _withdraw(poster, item)
        assert res.status_code == 200 and res.json() == {"status": "withdrawn"}
        item.refresh_from_db()
        assert item.status == ListingStatus.WITHDRAWN


@pytest.mark.django_db
def test_withdraw_is_two_step_when_people_have_asked():
    poster, a, b = person(), person(), person()
    live = listing(poster)
    ia = inquire(a, live); inquire(b, live)
    first = _withdraw(poster, live)
    assert first.status_code == 409 and first.json()["error"]["code"] == "has_active_inquiries"
    assert first.json()["error"]["details"]["active_inquiries"] == 2
    live.refresh_from_db(); assert live.status == ListingStatus.AVAILABLE
    second = _withdraw(poster, live, {"close_inquiries": True})
    assert second.status_code == 200 and second.json()["closed_inquiries"] == 2
    live.refresh_from_db(); ia.refresh_from_db()
    assert live.status == ListingStatus.WITHDRAWN
    assert ia.status == InquiryStatus.WITHDRAWN and ia.end_reason == EndReason.LISTING_WITHDRAWN
    assert ia.ended_by_account_id == poster.pk
    assert "listing_withdrawn" in types(a) and "listing_withdrawn" in types(b)


@pytest.mark.django_db
def test_withdraw_also_releases_a_reserved_applicant():
    poster, adopter = person(), person(member=True)
    live = listing(poster)
    inq = inquire(adopter, live)
    post(poster, inq, "screen"); post(poster, inq, "reserve")
    assert _withdraw(poster, live, {"close_inquiries": True}).status_code == 200
    inq.refresh_from_db(); live.refresh_from_db()
    assert inq.status == InquiryStatus.WITHDRAWN and inq.reserved_at is None
    assert live.status == ListingStatus.WITHDRAWN


@pytest.mark.django_db
def test_only_the_poster_can_withdraw_and_not_once_adopted_or_withdrawn():
    poster, other = person(), person()
    live = listing(poster)
    assert _withdraw(other, live).status_code == 403
    assert _withdraw(person(), listing(person(), verify=False, status=ListingStatus.DRAFT)).status_code == 403
    adopted = listing(poster, status=ListingStatus.ADOPTED)
    assert _withdraw(poster, adopted).json()["error"]["code"] == "already_adopted"
    gone = listing(poster, status=ListingStatus.WITHDRAWN)
    assert _withdraw(poster, gone).json()["error"]["code"] == "not_withdrawable"
    assert c(poster).post("/api/v1/listings/00000000-0000-0000-0000-000000000001/withdraw"
                          ).status_code == 404


@pytest.mark.django_db
def test_withdrawing_a_rescue_listing_frees_the_case_like_a_take_back():
    from django.contrib.gis.geos import Point

    from sagip.models import RescueCase, StrayReport, StrayStatus
    rescuer = person(member=True)
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
        status=StrayStatus.SAFE, geom=Point(121.05, 14.63, srid=4326))
    RescueCase.objects.create(report=report, claimed_by_account=rescuer)
    live = listing(rescuer, source_report=report)
    assert _withdraw(rescuer, live).status_code == 200


@pytest.mark.django_db
def test_withdrawing_an_unanswered_placement_cancels_it_and_tells_the_recipient():
    from django.contrib.gis.geos import Point

    from sagip.models import RescueCase, StrayReport, StrayStatus
    rescuer, recipient = person(member=True), person(member=True)
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
        status=StrayStatus.SAFE, geom=Point(121.05, 14.63, srid=4326))
    RescueCase.objects.create(report=report, claimed_by_account=rescuer)
    placed = c(rescuer).post(f"/api/v1/cases/{RescueCase.objects.get().pk}/place",
        {"recipient_email": recipient.email, "name": "Bruno", "adoption_fee": "0"}, format="json").json()
    res = c(rescuer).post(f"/api/v1/listings/{placed['listing_id']}/withdraw", {}, format="json")
    assert res.status_code == 200
    assert "placement_withdrawn" in types(recipient)


# ── AD10 · mark adopted elsewhere ───────────────────────────────────────────────
def _mark(who, the_listing):
    return c(who).post(f"/api/v1/listings/{the_listing.pk}/mark-adopted", {}, format="json")


@pytest.mark.django_db
def test_mark_adopted_closes_every_applicant_kindly_and_creates_no_pet():
    poster, a, b = person(), person(), person()
    live = listing(poster)
    ia = inquire(a, live); inquire(b, live)
    res = _mark(poster, live)
    assert res.status_code == 200 and res.json() == {"status": "adopted", "closed_inquiries": 2}
    live.refresh_from_db(); ia.refresh_from_db()
    assert live.status == ListingStatus.ADOPTED and live.adopted_by_account_id is None
    assert live.adopted_pet_id is None
    assert ia.status == InquiryStatus.DECLINED and ia.end_reason == EndReason.ANOTHER_ADOPTER_CHOSEN
    assert "inquiry_rejected" in types(a) and "inquiry_rejected" in types(b)


@pytest.mark.django_db
def test_mark_adopted_is_refused_while_someone_holds_the_reservation_and_for_non_posters():
    poster, adopter = person(), person(member=True)
    live = listing(poster)
    inq = inquire(adopter, live)
    post(poster, inq, "screen"); post(poster, inq, "reserve")
    res = _mark(poster, live)
    assert res.status_code == 409 and res.json()["error"]["code"] == "use_complete"
    assert _mark(person(), live).status_code == 403
    live.refresh_from_db(); assert live.status == ListingStatus.PENDING


@pytest.mark.django_db
def test_mark_adopted_resolves_a_rescue_report_and_refuses_a_draft_or_withdrawn_listing():
    poster = person()
    assert _mark(poster, listing(poster, status=ListingStatus.DRAFT)).status_code == 409
    assert _mark(poster, listing(poster, status=ListingStatus.WITHDRAWN)).status_code == 409
    from django.contrib.gis.geos import Point

    from sagip.models import RescueCase, StrayReport, StrayStatus
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
        status=StrayStatus.SAFE, geom=Point(121.05, 14.63, srid=4326))
    RescueCase.objects.create(report=report, claimed_by_account=poster)
    assert _mark(poster, listing(poster, source_report=report)).status_code == 200
    report.refresh_from_db(); assert report.status == StrayStatus.RESOLVED


@pytest.mark.django_db
def test_a_shelters_dashboard_counts_drafts_and_live_separately():
    s = shelter()
    listing(s, status=ListingStatus.DRAFT); listing(s); listing(s, status=ListingStatus.ADOPTED)
    counts = c(s).get("/api/v1/shelter/dashboard").json()["counts"]
    assert counts["draft_listings"] == 1 and counts["live_listings"] == 1 and counts["adopted"] == 1
