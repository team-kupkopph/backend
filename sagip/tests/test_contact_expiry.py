"""C2 (dev/test-plan-sagip.md §0.3) · owner decision 2026-10-01: a consented contact stays
visible for 7 days after the rescue is resolved — long enough to settle follow-ups (vet
receipts, a helper's supplies, checking in) — then disappears. Names remain. The same rule covers
the owner/finder contact on a resolved lost<->found match.
"""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from listings.models import Pet
from sagip.models import CaseStatusHistory, RescueCase, StrayReport
from verifications.models import AccountCapability


def _c(a):
    c = APIClient(); c.force_authenticate(user=a); return c


def _resolved_rescue(days_ago):
    reporter = AccountFactory()
    report = StrayReport.objects.create(
        reporter_account=reporter, species="dog", condition="healthy", status="reported",
        city="Marikina", geom=Point(121.10, 14.65, srid=4326), contact_share_consent=True)
    claimer = AccountFactory()
    AccountCapability.objects.create(account=claimer, capability="rescuer", status="approved")
    case_id = _c(claimer).post(f"/api/v1/reports/{report.pk}/claim").json()["case_id"]
    _c(claimer).post(f"/api/v1/cases/{case_id}/contact", {"share": True}, format="json")
    _c(claimer).post(f"/api/v1/cases/{case_id}/status", {"status": "resolved"}, format="json")
    when = timezone.now() - timezone.timedelta(days=days_ago)
    RescueCase.objects.filter(pk=case_id).update(resolved_at=when)
    CaseStatusHistory.objects.filter(report=report, status="resolved").update(changed_at=when)
    return report, reporter, claimer


def _people(report, viewer):
    return _c(viewer).get(f"/api/v1/reports/{report.pk}").json().get("people")


@pytest.mark.django_db
def test_contact_stays_for_a_week_after_resolution():
    report, reporter, claimer = _resolved_rescue(days_ago=6)
    [them] = _people(report, reporter)
    assert them["contact"]["email"] == claimer.email
    [reporter_entry] = _people(report, claimer)
    assert reporter_entry["contact"]["email"] == reporter.email


@pytest.mark.django_db
def test_after_a_week_only_names_remain():
    report, reporter, claimer = _resolved_rescue(days_ago=8)
    [them] = _people(report, reporter)
    assert them == {"role": "claimer", "display_name": claimer.display_name}
    [reporter_entry] = _people(report, claimer)
    assert "contact" not in reporter_entry


@pytest.mark.django_db
def test_an_open_rescue_is_unaffected():
    reporter = AccountFactory()
    report = StrayReport.objects.create(
        reporter_account=reporter, species="dog", condition="healthy", status="reported",
        city="Marikina", geom=Point(121.10, 14.65, srid=4326), contact_share_consent=True)
    claimer = AccountFactory()
    AccountCapability.objects.create(account=claimer, capability="rescuer", status="approved")
    _c(claimer).post(f"/api/v1/reports/{report.pk}/claim")
    StrayReport.objects.filter(pk=report.pk).update(
        created_at=timezone.now() - timezone.timedelta(days=30))
    [entry] = _people(report, claimer)
    assert "contact" in entry


@pytest.mark.django_db
@pytest.mark.parametrize("days_ago,visible", [(6, True), (8, False)])
def test_a_resolved_lost_and_found_match_follows_the_same_week(days_ago, visible):
    owner, finder = AccountFactory(), AccountFactory()
    pet = Pet.objects.create(owner_account=owner, name="Bruno", species="dog")
    lost = _c(owner).post("/api/v1/reports", {"report_type": "lost", "species": "dog",
                                              "pet_id": str(pet.pk), "lat": 14.65, "lng": 121.10,
                                              "city": "Marikina"}, format="json").json()["report_id"]
    _c(finder).post("/api/v1/reports", {"sighting_of": lost, "species": "dog", "condition": "healthy",
                                        "lat": 14.65, "lng": 121.10, "city": "Marikina",
                                        "contact_share_consent": True}, format="json")
    [m] = _c(owner).get(f"/api/v1/reports/{lost}/matches").json()["results"]
    _c(owner).post(f"/api/v1/reports/{lost}/matches/{m['match_id']}/confirm")
    when = timezone.now() - timezone.timedelta(days=days_ago)
    CaseStatusHistory.objects.filter(status="resolved").update(changed_at=when)

    [m] = _c(owner).get(f"/api/v1/reports/{lost}/matches").json()["results"]
    assert ("contact" in m["reporter"]) is visible
