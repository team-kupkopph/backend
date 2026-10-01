"""C12 / D9 · report-time alerts need a verified phone and a per-reporter daily cap."""
import itertools

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from accounts.models import Address
from notifications.models import Notification
from sagip.alerts import DAILY_CAP, REPORTER_DAILY_CAP
from sagip.models import StrayReport
from verifications.models import AccountCapability

_phones = itertools.count(1)


def _c(account):
    c = APIClient(); c.force_authenticate(user=account); return c


def _verified(city=None):
    a = AccountFactory()
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


def _phone_ok(a):
    # Account.phone is unique, so each reporter gets their own number.
    a.phone, a.phone_verified_at = f"+6391700{next(_phones):05d}", timezone.now(); a.save(); return a


def _rescuer_in_marikina():
    a = _verified()
    Address.objects.create(account=a, city="Marikina City", is_primary=True)
    return a


def _file(reporter, condition="injured"):
    body = {"species": "dog", "condition": condition, "lat": 14.65, "lng": 121.10, "city": "Marikina"}
    res = _c(reporter).post("/api/v1/reports", body, format="json")
    assert res.status_code == 201
    return StrayReport.objects.get(pk=res.json()["report_id"])


@pytest.mark.django_db
def test_an_unverified_phone_pages_no_one_and_the_reporter_is_told_why():
    rescuer = _rescuer_in_marikina()
    reporter = AccountFactory()                       # no verified phone
    report = _file(reporter)
    assert not Notification.objects.filter(account=rescuer, type="report_nearby").exists()
    report.refresh_from_db()
    assert report.alert_held == "phone_unverified"
    body = _c(reporter).get(f"/api/v1/reports/{report.pk}").json()
    assert body["escalation_notified"]["at_report_held"] == "phone_unverified"


@pytest.mark.django_db
def test_one_reporter_pages_people_for_at_most_three_reports_a_day():
    rescuer = _rescuer_in_marikina()
    reporter = _phone_ok(AccountFactory())
    reports = [_file(reporter) for _ in range(REPORTER_DAILY_CAP + 1)]
    assert Notification.objects.filter(account=rescuer, type="report_nearby").count() == REPORTER_DAILY_CAP
    reports[-1].refresh_from_db()
    assert reports[-1].alert_held == "reporter_cap"


@pytest.mark.django_db
def test_a_report_closed_as_a_mistake_gives_the_rescuer_their_alert_back():
    rescuer = _rescuer_in_marikina()
    reporters = [_phone_ok(AccountFactory()) for _ in range(DAILY_CAP)]
    filed = [_file(r) for r in reporters]
    assert _c(reporters[0]).post(f"/api/v1/reports/{filed[0].pk}/close",
                                 {"reason": "mistake"}, format="json").status_code == 200
    _file(_phone_ok(AccountFactory()))
    assert Notification.objects.filter(account=rescuer, type="report_nearby").count() == DAILY_CAP + 1
