import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from notifications.models import Notification
from sagip.models import CaseStatusHistory, RescueCase, StrayReport
from sagip.sweeps import _expire_case, _stalled_case_ids, reopen_case
from verifications.models import AccountCapability


def _c(account):
    c = APIClient(); c.force_authenticate(user=account); return c


def _verified(city=None):
    a = AccountFactory()
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


def _report(**kw):
    defaults = dict(reporter_account=AccountFactory(), species="dog", condition="injured",
                    status="reported", city="Marikina", geom=Point(121.10, 14.65, srid=4326))
    defaults.update(kw)
    return StrayReport.objects.create(**defaults)


def _claimed():
    report = _report()
    claimer = _verified()
    res = _c(claimer).post(f"/api/v1/reports/{report.pk}/claim")
    assert res.status_code == 201
    report.refresh_from_db()
    return report, RescueCase.objects.get(pk=res.json()["case_id"]), claimer


@pytest.mark.django_db
def test_a_claim_that_lapses_mid_request_is_not_moved_forward(monkeypatch):
    """C10a · the lapse commits between the view's first read and its write. The update must
    see the lapse, not write `rescued` onto a report whose claim just ended."""
    report, case, claimer = _claimed()
    import sagip.views as v
    real = v.CaseStatusUpdateSerializer

    class LapsesWhileValidating(real):
        def is_valid(self, *a, **kw):
            ok = super().is_valid(*a, **kw)
            stale = RescueCase.objects.select_related("report").get(pk=case.pk)
            reopen_case(stale, None, "Auto-expired: no update within the claim window")
            return ok

    monkeypatch.setattr(v, "CaseStatusUpdateSerializer", LapsesWhileValidating)
    res = _c(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": "rescued"}, format="json")

    assert res.status_code == 409 and res.json()["error"]["code"] == "case_expired"
    report.refresh_from_db()
    assert report.status == "reported"
    assert not CaseStatusHistory.objects.filter(report=report, status="rescued").exists()


def _age_claim(report, hours):
    CaseStatusHistory.objects.filter(report=report).update(
        changed_at=timezone.now() - timezone.timedelta(hours=hours))


@pytest.mark.django_db
def test_a_claim_updated_after_the_scan_is_not_lapsed():
    """C10b · the sweep found the claim stalled, then the rescuer posted "Rescued" before the
    sweep got to it. Reopening from the stale row would put an animal in custody back on the map."""
    report, case, claimer = _claimed()
    _age_claim(report, 7)
    now = timezone.now()
    assert _stalled_case_ids(now) == [case.pk]

    res = _c(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": "rescued"}, format="json")
    assert res.status_code == 200

    assert _expire_case(case.pk, now) is None
    report.refresh_from_db(); case.refresh_from_db()
    assert report.status == "rescued" and case.expired_at is None
    assert not Notification.objects.filter(type="claim_lapsed").exists()


@pytest.mark.django_db
def test_a_claim_released_after_the_scan_is_not_lapsed_twice():
    report, case, claimer = _claimed()
    _age_claim(report, 7)
    now = timezone.now()
    assert _stalled_case_ids(now) == [case.pk]
    assert _c(claimer).post(f"/api/v1/cases/{case.pk}/release",
                            {"reason": "cant_get_there"}, format="json").status_code == 200

    assert _expire_case(case.pk, now) is None
    assert CaseStatusHistory.objects.filter(report=report, status="reported").count() == 1
    assert Notification.objects.filter(account=report.reporter_account,
                                       type="case_reopened").count() == 1
