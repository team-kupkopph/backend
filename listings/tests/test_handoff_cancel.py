"""C14 / D11 · a rescuer can take back a handoff that hasn't happened yet, and an unanswered
placement doesn't hold the animal's handoff forever (it expires after 7 days; both sides hear)."""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from listings.models import AdoptionInquiry, AdoptionListing, InquiryStatus, ListingStatus
from listings.sweeps import PLACEMENT_EXPIRY_DAYS, expire_placements
from notifications.models import Notification
from sagip.models import RescueCase, StrayReport, StrayStatus
from verifications.models import AccountCapability


def _c(a):
    c = APIClient(); c.force_authenticate(user=a); return c


def _verified(a):
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


def _safe_case(rescuer):
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
        status=StrayStatus.SAFE, geom=Point(121.05, 14.63, srid=4326))
    return RescueCase.objects.create(report=report, claimed_by_account=rescuer)


def _place(rescuer, case, recipient):
    return _c(rescuer).post(f"/api/v1/cases/{case.pk}/place",
        {"recipient_email": recipient.email, "name": "Bruno", "adoption_fee": "0"}, format="json")


def _cancel(who, case):
    return _c(who).post(f"/api/v1/cases/{case.pk}/handoff/cancel")


@pytest.mark.django_db
def test_a_rescuer_can_take_back_an_unanswered_placement():
    rescuer, recipient = _verified(AccountFactory()), _verified(AccountFactory())
    case = _safe_case(rescuer)
    placed = _place(rescuer, case, recipient).json()
    res = _cancel(rescuer, case)
    assert res.status_code == 200
    inq = AdoptionInquiry.objects.get(pk=placed["inquiry_id"])
    assert inq.status == InquiryStatus.WITHDRAWN and inq.listing.status == ListingStatus.WITHDRAWN
    assert Notification.objects.filter(account=recipient, type="placement_withdrawn").count() == 1
    assert _place(rescuer, case, _verified(AccountFactory())).status_code == 201   # free again


@pytest.mark.django_db
def test_a_draft_can_be_cancelled():
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    lid = _c(rescuer).post(f"/api/v1/cases/{case.pk}/list", {}, format="json").json()["listing_id"]
    assert AdoptionListing.objects.get(pk=lid).status == ListingStatus.DRAFT
    assert _cancel(rescuer, case).status_code == 200
    assert AdoptionListing.objects.get(pk=lid).status == ListingStatus.WITHDRAWN


@pytest.mark.django_db
def test_a_public_listing_with_active_inquiries_cannot_be_cancelled():
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    lid = _c(rescuer).post(f"/api/v1/cases/{case.pk}/list", {}, format="json").json()["listing_id"]
    AdoptionListing.objects.filter(pk=lid).update(status=ListingStatus.AVAILABLE)
    AdoptionInquiry.objects.create(listing_id=lid, adopter_account=AccountFactory(),
                                   status=InquiryStatus.ACTIVE)
    res = _cancel(rescuer, case)
    assert res.status_code == 409 and res.json()["error"]["code"] == "has_active_inquiries"
    assert res.json()["error"]["message"] == ("People have asked about this animal, so it can't be "
                                              "taken down from here.")


@pytest.mark.django_db
def test_only_the_claimer_cancels_and_only_something_live():
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    assert _cancel(AccountFactory(), case).status_code == 403
    assert _cancel(rescuer, case).json()["error"]["code"] == "no_handoff"


@pytest.mark.django_db
def test_an_unanswered_placement_expires_after_seven_days_and_both_sides_hear():
    rescuer, recipient = _verified(AccountFactory()), _verified(AccountFactory())
    case = _safe_case(rescuer)
    placed = _place(rescuer, case, recipient).json()
    later = timezone.now() + timezone.timedelta(days=PLACEMENT_EXPIRY_DAYS, minutes=1)
    assert [str(i.pk) for i in expire_placements(now=later)] == [placed["inquiry_id"]]
    assert expire_placements(now=later) == []                       # idempotent
    [n] = Notification.objects.filter(account=rescuer, type="placement_decided")
    assert n.data["decision"] == "expired"
    assert Notification.objects.filter(account=recipient, type="placement_withdrawn").count() == 1


# ── A former claimer can't act on the next claimer's rescue ─────────────────────────────
def _released_then_reclaimed():
    """A claims and releases; B claims the same report, brings it to safe and places it. A's old
    case row survives (expired_at set) and still names A as its claimer."""
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
        status=StrayStatus.REPORTED, geom=Point(121.05, 14.63, srid=4326))
    a, b, recipient = (_verified(AccountFactory()) for _ in range(3))
    old = _c(a).post(f"/api/v1/reports/{report.pk}/claim").json()["case_id"]
    assert _c(a).post(f"/api/v1/cases/{old}/release", {"reason": "cant_get_there"},
                      format="json").status_code == 200
    new = _c(b).post(f"/api/v1/reports/{report.pk}/claim").json()["case_id"]
    for step in (StrayStatus.RESCUED, StrayStatus.SAFE):
        assert _c(b).post(f"/api/v1/cases/{new}/status", {"status": step},
                          format="json").status_code == 200
    placed = _place(b, RescueCase.objects.get(pk=new), recipient)
    assert placed.status_code == 201
    return a, RescueCase.objects.get(pk=old), placed.json(), recipient


def _assert_case_expired(res):
    assert res.status_code == 409
    err = res.json()["error"]
    assert (err["code"], err["message"]) == ("case_expired", "This claim has lapsed")


@pytest.mark.django_db
def test_a_former_claimer_cannot_cancel_the_next_claimers_placement():
    a, old_case, placed, _ = _released_then_reclaimed()
    _assert_case_expired(_cancel(a, old_case))
    inq = AdoptionInquiry.objects.get(pk=placed["inquiry_id"])
    assert inq.status == InquiryStatus.ACTIVE and inq.listing.status == ListingStatus.PENDING


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["list", "place"])
def test_a_former_claimer_cannot_list_or_place_from_a_lapsed_case(action):
    a, old_case, placed, _ = _released_then_reclaimed()
    # Withdrawn so the live-handoff guard can't be what refuses them.
    AdoptionListing.objects.filter(pk=placed["listing_id"]).update(status=ListingStatus.WITHDRAWN)
    if action == "list":
        res = _c(a).post(f"/api/v1/cases/{old_case.pk}/list", {}, format="json")
    else:
        res = _place(a, old_case, _verified(AccountFactory()))
    _assert_case_expired(res)
    assert AdoptionListing.objects.filter(source_report=old_case.report).count() == 1
