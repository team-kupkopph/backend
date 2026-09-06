"""US-F0/E1/E2 — the sweep functions, called directly (no scheduler in tests; that's the
whole point of US-F0's "rule = spec, job = build" split)."""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Address
from notifications.models import Notification
from sagip.models import CaseStatusHistory, OfferStatus, ReportOffer, RescueCase, StrayReport
from sagip.status import set_report_status
from sagip.sweeps import escalate_reports, expire_stalled_claims
from shelter.models import ShelterProfile
from verifications.models import AccountCapability, VerificationRequest

NOW = timezone.now()


def _report(**kw):
    defaults = dict(species="dog", condition="injured", status="reported",
                    geom=Point(121.05, 14.63, srid=4326))
    defaults.update(kw)
    r = StrayReport.objects.create(**defaults)
    if "created_at" in kw:
        StrayReport.objects.filter(pk=r.pk).update(created_at=kw["created_at"])
        r.refresh_from_db()
    return r


def _verified_rescuer_in(city):
    acc = AccountFactory()
    AccountCapability.objects.create(account=acc, capability="rescuer", status="approved",
                                     granted_at=NOW)
    Address.objects.create(account=acc, city=city, is_primary=True)
    return acc


def _escalation_partner_shelter():
    acc = AccountFactory(account_type="shelter")
    VerificationRequest.objects.create(account=acc, type="shelter_org", status="approved")
    ShelterProfile.objects.create(account=acc, org_name="Partner Org", org_type="shelter",
                                  tier="registered_ngo", is_escalation_partner=True)
    return acc


# ── US-E1 · escalation ──────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_escalates_to_level_1_past_a_third_of_the_injured_window():
    # injured window = 6h -> level 1 at 2h
    r = _report(condition="injured", created_at=NOW - timezone.timedelta(hours=2, minutes=1))
    escalate_reports(now=NOW)
    r.refresh_from_db()
    assert r.escalation_level == 1


@pytest.mark.django_db
def test_does_not_escalate_before_the_cadence():
    r = _report(condition="injured", created_at=NOW - timezone.timedelta(hours=1))
    escalate_reports(now=NOW)
    r.refresh_from_db()
    assert r.escalation_level == 0


@pytest.mark.django_db
def test_escalates_to_level_2_past_two_thirds_of_the_window():
    r = _report(condition="injured", created_at=NOW - timezone.timedelta(hours=4, minutes=1))
    escalate_reports(now=NOW)
    r.refresh_from_db()
    assert r.escalation_level == 2


@pytest.mark.django_db
def test_cadence_is_condition_scaled():
    # healthy window = 24h -> level 1 at 8h. Same age (3h) as the injured case above,
    # which already hit level 1 (its threshold is 2h) — healthy must not.
    r = _report(condition="healthy", created_at=NOW - timezone.timedelta(hours=3))
    escalate_reports(now=NOW)
    r.refresh_from_db()
    assert r.escalation_level == 0


@pytest.mark.django_db
def test_a_single_pass_can_cross_both_thresholds_and_fires_both_notifications():
    partner = _escalation_partner_shelter()
    r = _report(condition="injured", city="Marikina",
               created_at=NOW - timezone.timedelta(hours=5))  # past both 2h and 4h
    escalate_reports(now=NOW)
    r.refresh_from_db()
    assert r.escalation_level == 2
    types = list(Notification.objects.filter(account=partner).values_list("data", flat=True))
    levels = sorted(t["escalation_level"] for t in types)
    assert levels == [2]  # partner only qualifies for level 2's audience, not level 1's


@pytest.mark.django_db
def test_idempotent_second_pass_does_not_renotify():
    _r = _report(condition="injured", city="Marikina",
               created_at=NOW - timezone.timedelta(hours=3))
    rescuer = _verified_rescuer_in("Marikina")
    escalate_reports(now=NOW)
    first_count = Notification.objects.filter(account=rescuer, type="report_escalated").count()
    escalate_reports(now=NOW)  # same instant, run again
    second_count = Notification.objects.filter(account=rescuer, type="report_escalated").count()
    assert first_count == 1
    assert second_count == 1  # not re-fired


@pytest.mark.django_db
def test_halts_the_instant_a_report_is_claimed():
    r = _report(condition="injured", created_at=NOW - timezone.timedelta(hours=5))
    set_report_status(r, "claimed", AccountFactory())
    escalate_reports(now=NOW)
    r.refresh_from_db()
    assert r.escalation_level == 0  # never considered — status is no longer 'reported'


@pytest.mark.django_db
def test_level1_notifies_verified_rescuers_in_the_same_city_only():
    _r = _report(condition="injured", city="Marikina",
               created_at=NOW - timezone.timedelta(hours=3))
    same_city = _verified_rescuer_in("Marikina")
    other_city = _verified_rescuer_in("Pasig")
    unverified = AccountFactory()
    Address.objects.create(account=unverified, city="Marikina", is_primary=True)

    escalate_reports(now=NOW)
    assert Notification.objects.filter(account=same_city, type="report_escalated").exists()
    assert not Notification.objects.filter(account=other_city, type="report_escalated").exists()
    assert not Notification.objects.filter(account=unverified, type="report_escalated").exists()


@pytest.mark.django_db
def test_level1_with_no_resolved_city_still_advances_but_notifies_no_one():
    r = _report(condition="injured", city=None,
               created_at=NOW - timezone.timedelta(hours=3))
    escalate_reports(now=NOW)
    r.refresh_from_db()
    assert r.escalation_level == 1
    assert not Notification.objects.filter(type="report_escalated").exists()


@pytest.mark.django_db
def test_level2_notifies_only_approved_escalation_partner_shelters():
    _r = _report(condition="injured", city="Marikina",
               created_at=NOW - timezone.timedelta(hours=5))
    partner = _escalation_partner_shelter()
    non_partner = AccountFactory(account_type="shelter")
    VerificationRequest.objects.create(account=non_partner, type="shelter_org", status="approved")
    ShelterProfile.objects.create(account=non_partner, org_name="Regular Org",
                                  org_type="shelter", tier="registered_ngo",
                                  is_escalation_partner=False)

    escalate_reports(now=NOW)
    assert Notification.objects.filter(account=partner, type="report_escalated").exists()
    assert not Notification.objects.filter(account=non_partner, type="report_escalated").exists()


# ── US-E2 · stalled-claim auto-expiry ───────────────────────────────────────────────
def _stale_claim(hours_ago, condition="injured"):
    claimer = AccountFactory()
    r = _report(condition=condition, city="Marikina", status="claimed")
    case = RescueCase.objects.create(report=r, claimed_by_account=claimer)
    set_report_status(r, "claimed", claimer)
    CaseStatusHistory.objects.filter(report=r).update(
        changed_at=NOW - timezone.timedelta(hours=hours_ago))
    return case, r, claimer


@pytest.mark.django_db
def test_reverts_only_past_window_claims():
    stale_case, stale_report, _ = _stale_claim(hours_ago=7)   # injured window = 6h
    fresh_case, fresh_report, _ = _stale_claim(hours_ago=1)

    expire_stalled_claims(now=NOW)

    stale_case.refresh_from_db(); stale_report.refresh_from_db()
    fresh_case.refresh_from_db(); fresh_report.refresh_from_db()
    assert stale_case.expired_at is not None and stale_report.status == "reported"
    assert fresh_case.expired_at is None and fresh_report.status == "claimed"


@pytest.mark.django_db
def test_a_fresh_status_update_resets_the_clock():
    case, report, claimer = _stale_claim(hours_ago=7)
    set_report_status(report, "rescued", claimer)  # a NEW history row, right now
    expire_stalled_claims(now=NOW)
    case.refresh_from_db()
    assert case.expired_at is None  # the old stale row no longer matters


@pytest.mark.django_db
def test_expired_at_is_set_and_the_case_row_survives():
    case, report, _ = _stale_claim(hours_ago=7)
    case_id = case.pk
    expire_stalled_claims(now=NOW)
    assert RescueCase.objects.filter(pk=case_id).exists()
    case.refresh_from_db()
    assert case.expired_at == NOW


@pytest.mark.django_db
def test_history_records_the_revert():
    case, report, _ = _stale_claim(hours_ago=7)
    expire_stalled_claims(now=NOW)
    latest = CaseStatusHistory.objects.filter(report=report).order_by("-changed_at").first()
    assert latest.status == "reported" and latest.changed_by_account_id is None


@pytest.mark.django_db
def test_a_new_claim_is_allowed_after_expiry():
    case, report, _ = _stale_claim(hours_ago=7)
    expire_stalled_claims(now=NOW)
    new_claimer = AccountFactory()
    fresh = RescueCase.objects.create(report=report, claimed_by_account=new_claimer)
    assert fresh.expired_at is None
    assert report.cases.count() == 2


@pytest.mark.django_db
def test_case_reopened_reaches_every_prior_offerer_and_no_one_else():
    case, report, claimer = _stale_claim(hours_ago=7)
    offerer_a, offerer_b, stranger = AccountFactory(), AccountFactory(), AccountFactory()
    ReportOffer.objects.create(report=report, account=offerer_a, offer_type="transport",
                               status="matched", expires_at=NOW + timezone.timedelta(hours=10))
    ReportOffer.objects.create(report=report, account=offerer_b, offer_type="supplies",
                               status="expired", expires_at=NOW - timezone.timedelta(hours=1))

    expire_stalled_claims(now=NOW)

    assert Notification.objects.filter(account=offerer_a, type="case_reopened").exists()
    assert Notification.objects.filter(account=offerer_b, type="case_reopened").exists()
    assert not Notification.objects.filter(account=stranger, type="case_reopened").exists()
    assert not Notification.objects.filter(account=claimer, type="case_reopened").exists()


@pytest.mark.django_db
def test_a_still_valid_matched_offer_reverts_to_open():
    case, report, _ = _stale_claim(hours_ago=7)
    offer = ReportOffer.objects.create(report=report, account=AccountFactory(),
                                       offer_type="transport", status="matched",
                                       expires_at=NOW + timezone.timedelta(hours=10))
    expire_stalled_claims(now=NOW)
    offer.refresh_from_db()
    assert offer.status == OfferStatus.OPEN


@pytest.mark.django_db
def test_a_matched_offer_past_its_own_window_becomes_expired_not_open():
    case, report, _ = _stale_claim(hours_ago=7)
    offer = ReportOffer.objects.create(report=report, account=AccountFactory(),
                                       offer_type="transport", status="matched",
                                       expires_at=NOW - timezone.timedelta(hours=1))
    expire_stalled_claims(now=NOW)
    offer.refresh_from_db()
    assert offer.status == OfferStatus.EXPIRED


@pytest.mark.django_db
def test_resolved_cases_are_never_touched():
    claimer = AccountFactory()
    r = _report(condition="injured", status="resolved")
    case = RescueCase.objects.create(report=r, claimed_by_account=claimer)
    set_report_status(r, "resolved", claimer)
    CaseStatusHistory.objects.filter(report=r).update(
        changed_at=NOW - timezone.timedelta(hours=100))
    expire_stalled_claims(now=NOW)
    case.refresh_from_db()
    assert case.expired_at is None


@pytest.mark.django_db
def test_idempotent_a_second_pass_does_not_reexpire_the_same_case():
    case, report, _ = _stale_claim(hours_ago=7)
    expire_stalled_claims(now=NOW)
    first_history_count = CaseStatusHistory.objects.filter(report=report).count()
    expire_stalled_claims(now=NOW)  # already expired_at != NULL -> excluded
    second_history_count = CaseStatusHistory.objects.filter(report=report).count()
    assert first_history_count == second_history_count
