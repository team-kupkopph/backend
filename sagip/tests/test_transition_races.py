import pytest
from django.contrib.gis.geos import Point
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from sagip.models import CaseStatusHistory, RescueCase, StrayReport
from sagip.sweeps import reopen_case
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
