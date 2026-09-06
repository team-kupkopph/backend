import pytest
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from listings.models import AdoptionInquiry, AdoptionListing, AdoptionStage, StageState
from sagip.models import RescueCase, StrayReport, StrayStatus


def _c(a):
    c = APIClient(); c.force_authenticate(user=a); return c


def _safe_case(rescuer):
    from django.contrib.gis.geos import Point
    report = StrayReport.objects.create(species="dog", condition="injured",
        status=StrayStatus.SAFE, geom=Point(121.05, 14.63, srid=4326))
    return RescueCase.objects.create(report=report, claimed_by_account=rescuer)


def _verified(a):
    # Make `a` a Verified Member — an approved `rescuer` capability (the pattern accounts/listings
    # tests use: `AccountCapability.objects.create(account=..., capability="rescuer", status="approved")`).
    from verifications.models import AccountCapability
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


@pytest.mark.django_db
def test_place_with_verified_recipient_creates_listing_inquiry_all_stages_skipped():
    r = AccountFactory(); recipient = _verified(AccountFactory())
    case = _safe_case(r)
    res = _c(r).post(f"/api/v1/cases/{case.pk}/place",
                     {"recipient_email": recipient.email, "city": "Manila", "adoption_fee": "0"}, format="json")
    assert res.status_code == 201
    inq = AdoptionInquiry.objects.get(pk=res.json()["inquiry_id"])
    assert inq.adopter_account_id == recipient.pk and inq.status == "active"
    states = set(AdoptionStage.objects.filter(inquiry=inq).values_list("state", flat=True))
    assert states == {StageState.SKIPPED}          # every stage skipped — the placement bypass
    assert AdoptionListing.objects.get(pk=res.json()["listing_id"]).status == "pending"


@pytest.mark.django_db
def test_place_unverified_recipient_422_nothing_created():
    r = AccountFactory(); recipient = AccountFactory()   # NOT verified
    case = _safe_case(r)
    res = _c(r).post(f"/api/v1/cases/{case.pk}/place",
                     {"recipient_email": recipient.email, "city": "X", "adoption_fee": "0"}, format="json")
    assert res.status_code == 422 and res.json()["error"]["code"] == "recipient_not_verified"
    assert not AdoptionListing.objects.filter(source_report=case.report).exists()


@pytest.mark.django_db
def test_place_unknown_recipient_404():
    r = AccountFactory(); case = _safe_case(r)
    res = _c(r).post(f"/api/v1/cases/{case.pk}/place",
                     {"recipient_email": "nobody@example.com", "city": "X", "adoption_fee": "0"}, format="json")
    assert res.status_code == 404
