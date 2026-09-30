"""D3 (dev/sagip-build-review.md) · a claimer who can't make it can say so, and the animal is
back on the map at once instead of waiting out the claim window (6 h for an injured one).

Owner decision 2026-09-30: release with a reason. The report reopens immediately; the reporter
and the helpers are told; it is recorded like a lapse (expired_at set) for accountability, with
no penalty. Only a `claimed` case can be released — once the animal is rescued the claimer has
custody, and the way out of custody is a handoff, never a release back onto the street.
"""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from notifications.models import Notification
from sagip.models import CaseStatusHistory, OfferStatus, ReportOffer, RescueCase, StrayReport
from verifications.models import AccountCapability


def _c(account):
    c = APIClient(); c.force_authenticate(user=account); return c


def _verified():
    a = AccountFactory()
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


def _claimed():
    report = StrayReport.objects.create(
        reporter_account=AccountFactory(), species="dog", condition="injured", status="reported",
        city="Marikina", geom=Point(121.10, 14.65, srid=4326))
    claimer = _verified()
    res = _c(claimer).post(f"/api/v1/reports/{report.pk}/claim")
    assert res.status_code == 201
    report.refresh_from_db()
    return report, RescueCase.objects.get(pk=res.json()["case_id"]), claimer


def _release(case, who, reason="cant_get_there"):
    return _c(who).post(f"/api/v1/cases/{case.pk}/release", {"reason": reason}, format="json")


@pytest.mark.django_db
def test_releasing_reopens_the_report_at_once_and_records_it_like_a_lapse():
    report, case, claimer = _claimed()
    res = _release(case, claimer)
    assert res.status_code == 200 and res.json() == {"status": "reported"}

    case.refresh_from_db(); report.refresh_from_db()
    assert report.status == "reported"
    assert case.expired_at is not None                     # counted like a lapse
    last = CaseStatusHistory.objects.filter(report=report).latest("changed_at")
    assert last.status == "reported" and last.changed_by_account_id == claimer.pk
    assert last.note == "released_by_claimer:cant_get_there"


@pytest.mark.django_db
def test_the_reporter_and_the_helpers_are_told_the_claimer_is_not():
    report, _, _ = _claimed()
    helper = AccountFactory()
    ReportOffer.objects.create(report=report, account=helper, offer_type="transport",
                               status=OfferStatus.MATCHED,
                               expires_at=timezone.now() + timezone.timedelta(hours=40))
    case = report.cases.get(expired_at__isnull=True)
    _release(case, case.claimed_by_account)

    [to_reporter] = Notification.objects.filter(account=report.reporter_account, type="case_reopened")
    assert "couldn't make it" in to_reporter.body
    assert Notification.objects.filter(account=helper, type="case_reopened").count() == 1
    assert not Notification.objects.filter(account=case.claimed_by_account,
                                           type__in=["case_reopened", "claim_lapsed"]).exists()


@pytest.mark.django_db
def test_matched_help_is_available_again_unless_its_own_window_passed():
    report, case, claimer = _claimed()
    fresh = ReportOffer.objects.create(report=report, account=AccountFactory(), offer_type="transport",
                                       status=OfferStatus.MATCHED,
                                       expires_at=timezone.now() + timezone.timedelta(hours=40))
    stale = ReportOffer.objects.create(report=report, account=AccountFactory(), offer_type="supplies",
                                       status=OfferStatus.MATCHED,
                                       expires_at=timezone.now() - timezone.timedelta(hours=1))
    _release(case, claimer)
    fresh.refresh_from_db(); stale.refresh_from_db()
    assert fresh.status == OfferStatus.OPEN and stale.status == OfferStatus.EXPIRED


@pytest.mark.django_db
def test_someone_else_can_claim_it_straight_away():
    report, case, claimer = _claimed()
    _release(case, claimer)
    assert _c(_verified()).post(f"/api/v1/reports/{report.pk}/claim").status_code == 201


@pytest.mark.django_db
def test_the_released_claimer_loses_the_exact_spot_and_the_case():
    report, case, claimer = _claimed()
    _release(case, claimer)
    body = _c(claimer).get(f"/api/v1/reports/{report.pk}").json()
    assert "precise_location" not in body and "my_case" not in body and "people" not in body


@pytest.mark.django_db
@pytest.mark.parametrize("status,code", [("rescued", "in_custody"), ("safe", "in_custody"),
                                         ("resolved", "case_resolved")])
def test_a_case_past_claimed_cannot_be_released(status, code):
    """Once the animal is rescued the claimer has custody; releasing would put it back on the
    street. The way out of custody is a handoff (list or place), which D7 covers."""
    report, case, claimer = _claimed()
    _c(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": status}, format="json")
    res = _release(case, claimer)
    assert res.status_code == 409 and res.json()["error"]["code"] == code
    case.refresh_from_db()
    assert case.expired_at is None


@pytest.mark.django_db
def test_only_the_claimer_can_release():
    _, case, _ = _claimed()
    assert _release(case, AccountFactory()).status_code == 403
    case.refresh_from_db()
    assert case.expired_at is None


@pytest.mark.django_db
def test_a_lapsed_claim_cannot_be_released_again():
    _, case, claimer = _claimed()
    _release(case, claimer)
    res = _release(case, claimer)
    assert res.status_code == 409 and res.json()["error"]["code"] == "case_expired"


@pytest.mark.django_db
def test_a_release_needs_a_known_reason():
    _, case, claimer = _claimed()
    assert _release(case, claimer, reason="bored").status_code in (400, 422)
    case.refresh_from_db()
    assert case.expired_at is None
