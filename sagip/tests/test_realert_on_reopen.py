"""Re-alert on reopen · when a claimed report goes back on the map — its claimer released it (D3)
or the claim lapsed — the people who haven't been asked about it yet are paged.

Before this, D2's alert fired only when a report was first filed, and escalation had usually run
its course by the time a claim failed. An injured animal reopened at hour 5 relied on whoever
happened to open the map. Same policy as D2: urgent strays and found animals only, verified
rescuers and shelters in the city, the same 5-per-24h cap. Nobody already asked about this
report is asked again, nor the reporter, nor the claimer who just let go.
"""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from accounts.models import Address
from notifications.models import Notification
from sagip.alerts import ALERT_TYPE
from sagip.models import CaseStatusHistory, RescueCase, StrayReport
from sagip.sweeps import escalate_reports, expire_stalled_claims
from shelter.models import ShelterProfile
from verifications.models import AccountCapability, VerificationRequest


def _c(a):
    c = APIClient(); c.force_authenticate(user=a); return c


def _rescuer(city="Marikina City"):
    a = AccountFactory()
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    Address.objects.create(account=a, city=city, is_primary=True)
    return a


def _shelter(city="Marikina City"):
    a = AccountFactory(account_type="shelter")
    VerificationRequest.objects.create(account=a, type="shelter_org", status="approved")
    ShelterProfile.objects.create(account=a, org_name="Org", org_type="shelter", tier="registered_ngo")
    Address.objects.create(account=a, city=city, is_primary=True)
    return a


def _filed(reporter, condition="injured"):
    # D9 · alerts need a verified phone: every reporter in this file has one.
    reporter.phone_verified_at = timezone.now(); reporter.save(update_fields=["phone_verified_at"])
    res = _c(reporter).post("/api/v1/reports", {"species": "dog", "condition": condition,
                                                "lat": 14.6507, "lng": 121.1029, "city": "Marikina"},
                            format="json")
    assert res.status_code == 201
    return StrayReport.objects.get(pk=res.json()["report_id"])


def _claim(report, claimer):
    res = _c(claimer).post(f"/api/v1/reports/{report.pk}/claim")
    assert res.status_code == 201
    return RescueCase.objects.get(pk=res.json()["case_id"])


def _reopened_alerts(account):
    return Notification.objects.filter(account=account, type=ALERT_TYPE, data__reopened=True)


@pytest.mark.django_db
def test_a_release_pages_the_people_not_yet_asked():
    reporter, asked_at_report = AccountFactory(), _rescuer()
    report = _filed(reporter)                        # D2 pages `asked_at_report`
    claimer = _rescuer()                             # verified after filing: never asked
    case = _claim(report, claimer)
    newcomer, shelter = _rescuer(), _shelter()       # joined since

    _c(claimer).post(f"/api/v1/cases/{case.pk}/release", {"reason": "cant_get_there"}, format="json")

    for who in (newcomer, shelter):
        [n] = _reopened_alerts(who)
        assert n.data == {"report_id": str(report.pk), "reopened": True}
        assert "again" in n.title
    for who in (asked_at_report, claimer, reporter):
        assert not _reopened_alerts(who).exists()


@pytest.mark.django_db
def test_a_lapse_pages_them_too():
    report = _filed(AccountFactory())
    claimer = _rescuer()
    _claim(report, claimer)
    newcomer = _rescuer()
    CaseStatusHistory.objects.filter(report=report).update(
        changed_at=timezone.now() - timezone.timedelta(hours=7))
    expire_stalled_claims()
    assert _reopened_alerts(newcomer).count() == 1
    assert not _reopened_alerts(claimer).exists()


@pytest.mark.django_db
def test_someone_already_asked_by_escalation_is_not_asked_again():
    report = StrayReport.objects.create(
        reporter_account=AccountFactory(), species="dog", condition="injured", status="reported",
        city="Marikina", geom=Point(121.1029, 14.6507, srid=4326))
    escalated = _rescuer()
    StrayReport.objects.filter(pk=report.pk).update(
        created_at=timezone.now() - timezone.timedelta(hours=3))
    escalate_reports()                                # level 1 reaches `escalated`
    claimer = _rescuer()
    case = _claim(report, claimer)
    _c(claimer).post(f"/api/v1/cases/{case.pk}/release", {"reason": "cant_find"}, format="json")
    assert not _reopened_alerts(escalated).exists()


@pytest.mark.django_db
def test_a_healthy_stray_reopens_quietly():
    """D2's policy: a healthy stray is map-only, when filed and when reopened."""
    report = _filed(AccountFactory(), condition="healthy")
    claimer = _rescuer()
    case = _claim(report, claimer)
    newcomer = _rescuer()
    _c(claimer).post(f"/api/v1/cases/{case.pk}/release", {"reason": "cant_get_there"}, format="json")
    assert not _reopened_alerts(newcomer).exists()


@pytest.mark.django_db
def test_the_daily_cap_still_applies():
    report = _filed(AccountFactory())
    claimer = _rescuer()
    case = _claim(report, claimer)
    busy = _rescuer()
    for _ in range(5):
        Notification.objects.create(account=busy, type=ALERT_TYPE, data={"report_id": "other"})
    _c(claimer).post(f"/api/v1/cases/{case.pk}/release", {"reason": "cant_get_there"}, format="json")
    assert not _reopened_alerts(busy).exists()


@pytest.mark.django_db
def test_the_reporter_is_told_how_many_were_alerted_again_separately():
    reporter = AccountFactory()
    _rescuer()
    report = _filed(reporter)                         # 1 alerted at report time
    claimer = _rescuer()
    case = _claim(report, claimer)
    _rescuer(); _rescuer()                            # 2 newcomers
    _c(claimer).post(f"/api/v1/cases/{case.pk}/release", {"reason": "cant_get_there"}, format="json")

    counts = _c(reporter).get(f"/api/v1/reports/{report.pk}").json()["escalation_notified"]
    assert counts["at_report"] == 1                  # "alerted right away" stays true
    assert counts["reopened"] == 2
