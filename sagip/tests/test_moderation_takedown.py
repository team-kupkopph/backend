"""C13 / D10 · actioning a moderation flag on a Sagip report hides it everywhere (the row
survives as the audit trail), ends an unacted claim and tells the claimer, and leaves an animal
already in someone's care with them."""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from moderation.actions import resolve_flag
from moderation.models import FlagStatus, FlagTarget, ModerationFlag
from notifications.models import Notification
from sagip.models import OfferStatus, ReportOffer, RescueCase, StrayReport
from sagip.sweeps import escalate_reports
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


def _action(report):
    flag = ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type=FlagTarget.REPORT,
                                         target_id=report.pk, reason="fake")
    return resolve_flag(flag, AccountFactory(), FlagStatus.ACTIONED)


@pytest.mark.django_db
def test_actioning_a_report_flag_takes_it_off_every_public_surface():
    report = _report()
    _, effects = _action(report)
    assert effects == {"report_hidden": str(report.pk)}
    report.refresh_from_db()
    assert report.hidden_at is not None

    assert APIClient().get(f"/api/v1/reports/{report.pk}").status_code == 404
    rows = APIClient().get("/api/v1/reports/map?city=Marikina").json()["reports"]
    assert str(report.pk) not in [r["report_id"] for r in rows]
    mine = _c(report.reporter_account).get(f"/api/v1/reports/{report.pk}").json()
    assert mine["hidden"] is True
    assert _c(_verified()).post(f"/api/v1/reports/{report.pk}/claim").status_code == 404


@pytest.mark.django_db
def test_a_hidden_report_stops_escalating_and_its_offers_expire():
    report = _report()
    offer = ReportOffer.objects.create(report=report, account=AccountFactory(), offer_type="transport",
                                       status=OfferStatus.OPEN,
                                       expires_at=timezone.now() + timezone.timedelta(hours=40))
    _action(report)
    offer.refresh_from_db()
    assert offer.status == OfferStatus.EXPIRED
    StrayReport.objects.filter(pk=report.pk).update(created_at=timezone.now() - timezone.timedelta(hours=5))
    assert escalate_reports() == []


@pytest.mark.django_db
def test_a_claimed_case_ends_and_the_claimer_is_told_but_custody_is_left_alone():
    report, case, claimer = _claimed()
    _action(report)
    case.refresh_from_db()
    assert case.expired_at is not None
    [n] = Notification.objects.filter(account=claimer, type="report_removed")
    assert n.data == {"report_id": str(report.pk)}

    kept, kept_case, keeper = _claimed()
    assert _c(keeper).post(f"/api/v1/cases/{kept_case.pk}/status", {"status": "rescued"},
                           format="json").status_code == 200
    _action(kept)
    kept_case.refresh_from_db(); kept.refresh_from_db()
    assert kept_case.expired_at is None and kept.status == "rescued"
    assert _c(keeper).get(f"/api/v1/reports/{kept.pk}").status_code == 200   # active claimer still sees it
