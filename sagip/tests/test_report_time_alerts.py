"""D2 / S6 (dev/sagip-build-review.md) · alert nearby help the moment an urgent stray is reported.

Before this, nobody was told when a report was filed: the first push went out at escalation
level 1, a third of the claim window later — two hours for an injured animal. Owner decision
D2 (2026-09-30): an injured, sick or pregnant animal alerts verified rescuers AND verified
shelters in the report's city at once, at most 5 of these alerts per person per 24 h. A
healthy stray stays map-only until the normal escalation.
"""
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from accounts.models import Address
from notifications.models import Notification
from sagip.models import StrayReport
from sagip.sweeps import escalate_reports
from shelter.models import ShelterProfile
from verifications.models import AccountCapability, VerificationRequest


def _c(account):
    c = APIClient(); c.force_authenticate(user=account); return c


def _in(account, city):
    Address.objects.create(account=account, city=city, is_primary=True)
    return account


def _rescuer(city="Marikina City"):
    a = AccountFactory()
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return _in(a, city)


def _shelter(city="Marikina City"):
    a = AccountFactory(account_type="shelter")
    VerificationRequest.objects.create(account=a, type="shelter_org", status="approved")
    ShelterProfile.objects.create(account=a, org_name="Org", org_type="shelter",
                                  tier="registered_ngo")
    return _in(a, city)


def _file(reporter, condition="injured", city="Marikina", **extra):
    # D9 · alerts need a verified phone: every reporter in this file has one.
    reporter.phone_verified_at = timezone.now(); reporter.save(update_fields=["phone_verified_at"])
    body = {"species": "dog", "condition": condition, "lat": 14.6507, "lng": 121.1029,
            "city": city}
    body.update(extra)
    res = _c(reporter).post("/api/v1/reports", body, format="json")
    assert res.status_code in (200, 201), res.content
    return StrayReport.objects.get(pk=res.json()["report_id"])


def _alerted(account):
    return Notification.objects.filter(account=account, type="report_nearby")


@pytest.mark.django_db
@pytest.mark.parametrize("condition", ["injured", "sick", "pregnant"])
def test_an_urgent_report_alerts_verified_rescuers_and_shelters_in_the_city(condition):
    rescuer, shelter = _rescuer(), _shelter()
    report = _file(AccountFactory(), condition=condition)
    for who in (rescuer, shelter):
        [n] = _alerted(who)
        assert n.data == {"report_id": str(report.pk)}


@pytest.mark.django_db
def test_a_healthy_stray_alerts_no_one_it_waits_for_escalation():
    rescuer = _rescuer()
    _file(AccountFactory(), condition="healthy")
    assert not _alerted(rescuer).exists()


@pytest.mark.django_db
def test_only_verified_accounts_in_the_same_city_are_alerted():
    elsewhere = _rescuer("Pasig City")
    unverified = _in(AccountFactory(), "Marikina City")
    pending_shelter = _in(AccountFactory(account_type="shelter"), "Marikina City")
    VerificationRequest.objects.create(account=pending_shelter, type="shelter_org", status="pending")
    suspended = _rescuer()
    suspended.status = "suspended"; suspended.save(update_fields=["status"])

    _file(AccountFactory())
    for who in (elsewhere, unverified, pending_shelter, suspended):
        assert not _alerted(who).exists()


@pytest.mark.django_db
def test_the_city_matches_however_it_is_spelled():
    rescuer = _rescuer("Marikina City")
    _file(AccountFactory(), city="marikina")
    assert _alerted(rescuer).count() == 1


@pytest.mark.django_db
def test_the_reporter_is_never_alerted_about_their_own_report():
    reporter = _rescuer()
    _file(reporter)
    assert not _alerted(reporter).exists()


@pytest.mark.django_db
def test_at_most_five_alerts_per_person_per_day():
    rescuer = _rescuer()
    for _ in range(6):
        _file(AccountFactory())
    assert _alerted(rescuer).count() == 5


@pytest.mark.django_db
def test_alerts_older_than_a_day_do_not_count_toward_the_cap():
    rescuer = _rescuer()
    for _ in range(5):
        _file(AccountFactory())
    _alerted(rescuer).update(created_at=timezone.now() - timezone.timedelta(hours=25))
    _file(AccountFactory())
    assert _alerted(rescuer).count() == 6


@pytest.mark.django_db
def test_an_offline_retry_of_the_same_report_alerts_once():
    rescuer, reporter = _rescuer(), AccountFactory()
    _file(reporter, idempotency_key="k-1")
    _file(reporter, idempotency_key="k-1")          # the outbox replaying it
    assert _alerted(rescuer).count() == 1


@pytest.mark.django_db
def test_a_report_with_no_city_alerts_no_one_and_still_files():
    # C8: only when no known city is near the point (here, Cebu). Near one, the server derives
    # the city and the alert goes out — test_city_resolution.py.
    rescuer = _rescuer()
    report = _file(AccountFactory(), city="", lat=10.3157, lng=123.8854)
    assert report.city is None
    assert not _alerted(rescuer).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("report_type,alerted", [("found", True), ("lost", False)])
def test_a_found_animal_alerts_a_lost_pet_does_not(report_type, alerted):
    """D6: a lost pet is its owner's to find, and isn't claimable; a FOUND animal is loose and
    needs someone to hold it, the same as a stray."""
    rescuer = _rescuer()
    _file(AccountFactory(), report_type=report_type)
    assert _alerted(rescuer).exists() is alerted


@pytest.mark.django_db
def test_a_failed_alert_never_breaks_the_report(monkeypatch):
    import sagip.alerts

    def boom(*a, **k):
        raise RuntimeError("push provider down")

    monkeypatch.setattr(sagip.alerts, "notify", boom)
    _rescuer()
    reporter = AccountFactory()
    res = _c(reporter).post("/api/v1/reports", {"species": "dog", "condition": "injured",
                                                "lat": 14.65, "lng": 121.10, "city": "Marikina"},
                            format="json")
    assert res.status_code == 201


@pytest.mark.django_db
def test_the_reporter_sees_how_many_were_alerted_at_report_time():
    _rescuer(); _rescuer(); _shelter()
    reporter = AccountFactory()
    report = _file(reporter)
    body = _c(reporter).get(f"/api/v1/reports/{report.pk}").json()
    assert body["escalation_notified"]["at_report"] == 3


@pytest.mark.django_db
def test_a_healthy_report_says_no_one_was_alerted_by_policy_not_by_absence():
    """`at_report` is null when the policy sends nothing (a healthy stray), so the app never
    says "no one there to alert" when the truth is "we don't alert for this"."""
    _rescuer()
    reporter = AccountFactory()
    report = _file(reporter, condition="healthy")
    body = _c(reporter).get(f"/api/v1/reports/{report.pk}").json()
    assert body["escalation_notified"]["at_report"] is None


# ── level 1 now reaches shelters too, and doesn't repeat itself ─────────────────────────
@pytest.mark.django_db
def test_level1_skips_anyone_already_alerted_at_report_time():
    alerted_early = _rescuer()
    report = _file(AccountFactory())
    late_joiner = _rescuer()                        # verified after the report was filed
    StrayReport.objects.filter(pk=report.pk).update(
        created_at=timezone.now() - timezone.timedelta(hours=3))

    escalate_reports()
    assert not Notification.objects.filter(account=alerted_early, type="report_escalated").exists()
    assert Notification.objects.filter(account=late_joiner, type="report_escalated").exists()


@pytest.mark.django_db
def test_level1_reaches_verified_shelters_in_the_city():
    """S16's alert half: a verified shelter in the report's city was skipped until level 2,
    and then only if it was an escalation partner."""
    shelter = _shelter()
    report = _file(AccountFactory(), condition="healthy")   # healthy: nothing at report time
    StrayReport.objects.filter(pk=report.pk).update(
        created_at=timezone.now() - timezone.timedelta(hours=9))   # healthy level 1 = 8 h

    escalate_reports()
    assert Notification.objects.filter(account=shelter, type="report_escalated").exists()
