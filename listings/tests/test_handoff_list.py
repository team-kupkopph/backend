import pytest
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from listings.models import AdoptionListing
from sagip.models import RescueCase, StrayReport, StrayStatus


def _c(a):
    c = APIClient(); c.force_authenticate(user=a); return c


def _safe_case(rescuer, *, status=StrayStatus.SAFE):
    # A stray report + a case claimed by the rescuer — mirrors sagip/tests/test_case_status_view.py
    # (report_type defaults; condition + geom are required; reporter_account is nullable).
    from django.contrib.gis.geos import Point
    report = StrayReport.objects.create(species="dog", condition="injured",
        status=status, geom=Point(121.05, 14.63, srid=4326))
    return RescueCase.objects.create(report=report, claimed_by_account=rescuer)


@pytest.mark.django_db
def test_list_from_safe_own_case_creates_listing_with_source_report():
    r = AccountFactory()
    case = _safe_case(r)
    body = {"city": "Manila", "adoption_fee": "300.00", "name": "Bantay"}
    res = _c(r).post(f"/api/v1/cases/{case.pk}/list", body, format="json")
    assert res.status_code == 201
    listing = AdoptionListing.objects.get(pk=res.json()["listing_id"])
    assert listing.source_report_id == case.report_id      # carries the report
    assert listing.posted_by_id == r.pk and listing.species == "dog"   # inherited species


@pytest.mark.django_db
def test_list_non_owner_403():
    owner = AccountFactory(); other = AccountFactory()
    case = _safe_case(owner)
    res = _c(other).post(f"/api/v1/cases/{case.pk}/list", {"city": "X", "adoption_fee": "0"}, format="json")
    assert res.status_code == 403


@pytest.mark.django_db
def test_list_non_safe_case_409():
    r = AccountFactory()
    case = _safe_case(r, status=StrayStatus.CLAIMED)
    res = _c(r).post(f"/api/v1/cases/{case.pk}/list", {"city": "X", "adoption_fee": "0"}, format="json")
    assert res.status_code == 409 and res.json()["error"]["code"] == "case_not_safe"


@pytest.mark.django_db
def test_list_over_fee_cap_422_no_listing():
    r = AccountFactory()   # a personal account is fee-capped (fee_cap_for returns FEE_CAP)
    case = _safe_case(r)
    res = _c(r).post(f"/api/v1/cases/{case.pk}/list", {"city": "X", "adoption_fee": "999999"}, format="json")
    assert res.status_code == 422 and res.json()["error"]["code"] == "fee_over_cap"
    assert not AdoptionListing.objects.filter(source_report=case.report).exists()


@pytest.mark.django_db
def test_list_no_city_and_report_city_none_defaults_to_empty_string():
    # StrayReport.city is nullable and unset here; the caller also omits city.
    # AdoptionListing.city is non-nullable — must fall back to "", not None (no 500).
    r = AccountFactory()
    case = _safe_case(r)
    assert case.report.city is None
    res = _c(r).post(f"/api/v1/cases/{case.pk}/list", {"adoption_fee": "0"}, format="json")
    assert res.status_code == 201
    listing = AdoptionListing.objects.get(pk=res.json()["listing_id"])
    assert listing.city == ""
