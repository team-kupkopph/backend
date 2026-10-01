import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from notifications.models import Notification
from sagip.models import CaseStatusHistory, ReportOffer, RescueCase, StrayReport
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


@pytest.mark.django_db
def test_an_offer_racing_a_claim_is_refused(monkeypatch):
    report = _report()
    rescuer, helper = _verified(), AccountFactory()
    import sagip.views as v
    real = v.OfferCreateSerializer

    class ClaimedWhileValidating(real):
        def is_valid(self, *a, **kw):
            ok = super().is_valid(*a, **kw)
            assert _c(rescuer).post(f"/api/v1/reports/{report.pk}/claim").status_code == 201
            return ok

    monkeypatch.setattr(v, "OfferCreateSerializer", ClaimedWhileValidating)
    res = _c(helper).post(f"/api/v1/reports/{report.pk}/offers",
                          {"offer_type": "transport"}, format="json")

    assert res.status_code == 409 and res.json()["error"]["code"] == "report_not_open"
    assert not ReportOffer.objects.filter(report=report).exists()


# ── R1 · one lock order: a takedown and a match decision ────────────────────────────────
def _pair():
    lost = _report(report_type="lost", condition="healthy")
    found = _report(report_type="found", condition="healthy")
    from sagip.models import ReportMatch
    return lost, found, ReportMatch.objects.create(report=found, matched_report=lost, score="0.900")


def _locked_tables(ctx):
    """The table each `SELECT … FOR UPDATE` in `ctx` locks, in the order they ran."""
    import re
    return [re.search(r'FROM "(\w+)"', q["sql"]).group(1)
            for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]


@pytest.mark.django_db
@pytest.mark.parametrize("gone", ["taken_down", "deleted"])
def test_a_takedown_committed_just_before_a_match_confirm_is_404(monkeypatch, gone):
    """R1 · the confirm passed its unlocked hidden check, then a takedown (or a delete) committed
    before it locked anything. Under the locks it must see that and refuse with the same 404 —
    not resolve the other reporter's real report off a removed one, and not a 500."""
    from moderation.actions import resolve_flag
    from moderation.models import FlagStatus, FlagTarget, ModerationFlag
    from sagip import views
    from sagip.models import MatchStatus, ReportMatch
    lost, found, match = _pair()
    real_atomic, fired = views.transaction.atomic, []

    def gone_first(*a, **kw):
        if not fired:
            fired.append(True)
            if gone == "taken_down":
                flag = ModerationFlag.objects.create(reporter_account=AccountFactory(),
                    target_type=FlagTarget.REPORT, target_id=found.pk, reason="fake")
                resolve_flag(flag, AccountFactory(), FlagStatus.ACTIONED)
            else:
                StrayReport.objects.filter(pk=found.pk).delete()
        return real_atomic(*a, **kw)
    from types import SimpleNamespace
    monkeypatch.setattr(views, "transaction", SimpleNamespace(atomic=gone_first))

    res = _c(lost.reporter_account).post(f"/api/v1/reports/{lost.pk}/matches/{match.pk}/confirm")

    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"
    lost.refresh_from_db()
    assert lost.status == "reported"
    assert not CaseStatusHistory.objects.filter(report=lost, status="resolved").exists()
    if gone == "taken_down":
        found.refresh_from_db()
        match.refresh_from_db()
        assert found.status == "reported" and found.hidden_at is not None
        assert match.status == MatchStatus.DISMISSED      # the takedown's own write, not ours
    else:
        assert not ReportMatch.objects.filter(pk=match.pk).exists()


@pytest.mark.django_db
def test_a_match_decision_locks_cases_then_reports_then_the_match():
    """R1 · the same order as hide_report (case -> report, then its matches), so a takedown and
    a confirm on the same pair queue instead of deadlocking."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    lost, found, match = _pair()
    RescueCase.objects.create(report=found, claimed_by_account=_verified())
    with CaptureQueriesContext(connection) as ctx:
        res = _c(lost.reporter_account).post(
            f"/api/v1/reports/{lost.pk}/matches/{match.pk}/dismiss")
    assert res.status_code == 200
    assert _locked_tables(ctx) == ["rescue_case", "stray_report", "report_match"]
