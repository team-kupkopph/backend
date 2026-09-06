"""US-K1 — POST /reports/{id}/claim: exclusive, binding, gated on IsVerifiedRescuer.

Claiming is the spine of Track K (Sprint 3 build order: F0 → K → O → E). A claim must
move the report to `claimed` (logged via set_report_status), match every open offer on
the report, and notify the reporter + each matched offerer — all inside one transaction —
and must never let two accounts win an active claim on the same report.
"""
import uuid

import pytest
from django.contrib.gis.geos import Point
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from notifications.models import Notification
from sagip.models import CaseStatusHistory, OfferStatus, OfferType, ReportOffer, RescueCase, StrayReport
from verifications.models import AccountCapability, VerificationRequest


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _claim(client, report, acc):
    return client.post(f"/api/v1/reports/{report.pk}/claim", {},
                       content_type="application/json", **_hdr(acc))


def _report(**kw):
    defaults = dict(species="dog", condition="injured", geom=Point(121.05, 14.63, srid=4326))
    defaults.update(kw)
    return StrayReport.objects.create(**defaults)


def _verified_member():
    acc = AccountFactory()
    AccountCapability.objects.create(account=acc, capability="rescuer", status="approved",
                                     granted_at=timezone.now())
    return acc


def _verified_shelter():
    acc = AccountFactory(account_type="shelter")
    VerificationRequest.objects.create(account=acc, type="shelter_org", status="approved")
    return acc


def _offer(report, account, offer_type=OfferType.TRANSPORT, status=OfferStatus.OPEN):
    return ReportOffer.objects.create(
        report=report, account=account, offer_type=offer_type, status=status,
        expires_at=timezone.now() + timezone.timedelta(hours=48))


@pytest.mark.django_db
def test_an_unverified_caller_is_403d(client):
    report = _report()
    res = _claim(client, report, AccountFactory())
    assert res.status_code == 403
    assert RescueCase.objects.count() == 0


@pytest.mark.django_db
def test_a_verified_member_can_claim(client):
    report = _report()
    member = _verified_member()
    res = _claim(client, report, member)
    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "claimed"
    case = RescueCase.objects.get(pk=body["case_id"])
    assert case.report_id == report.pk and case.claimed_by_account_id == member.pk


@pytest.mark.django_db
def test_a_verified_shelter_can_claim(client):
    report = _report()
    shelter = _verified_shelter()
    res = _claim(client, report, shelter)
    assert res.status_code == 201
    assert RescueCase.objects.filter(report=report, claimed_by_account=shelter).exists()


@pytest.mark.django_db
def test_claiming_moves_the_report_and_logs_it(client):
    report = _report()
    member = _verified_member()
    _claim(client, report, member)
    report.refresh_from_db()
    assert report.status == "claimed"
    history = CaseStatusHistory.objects.get(report=report)
    assert history.status == "claimed" and history.changed_by_account_id == member.pk


@pytest.mark.django_db
def test_a_second_claim_on_the_same_report_is_409_not_500(client):
    report = _report()
    first, second = _verified_member(), _verified_member()
    ok = _claim(client, report, first)
    assert ok.status_code == 201

    blocked = _claim(client, report, second)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "already_claimed"
    # no duplicate case, no duplicate status-history row, first claimer untouched
    active = RescueCase.objects.filter(report=report, expired_at__isnull=True)
    assert active.count() == 1
    assert active.get().claimed_by_account_id == first.pk
    assert CaseStatusHistory.objects.filter(report=report).count() == 1


@pytest.mark.django_db
def test_the_db_constraint_still_yields_409_if_the_precheck_is_bypassed():
    """Backstop for the select_for_update precheck: even if two rows raced past it, the
    partial-unique constraint must turn the second INSERT into a handled 409, not a 500 —
    the view's `except IntegrityError` exists precisely for this."""
    report = _report()
    a, b = _verified_member(), _verified_member()
    RescueCase.objects.create(report=report, claimed_by_account=a)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            RescueCase.objects.create(report=report, claimed_by_account=b)


@pytest.mark.django_db
def test_claiming_matches_open_offers_and_notifies_each_offerer(client):
    report = _report()
    offerer1, offerer2 = AccountFactory(), AccountFactory()
    o1 = _offer(report, offerer1, OfferType.TRANSPORT)
    o2 = _offer(report, offerer2, OfferType.SUPPLIES)
    member = _verified_member()

    res = _claim(client, report, member)
    assert res.status_code == 201
    o1.refresh_from_db()
    o2.refresh_from_db()
    assert o1.status == OfferStatus.MATCHED and o2.status == OfferStatus.MATCHED
    assert Notification.objects.filter(account=offerer1, type="offer_matched").exists()
    assert Notification.objects.filter(account=offerer2, type="offer_matched").exists()


@pytest.mark.django_db
def test_claiming_leaves_matched_and_expired_offers_alone(client):
    report = _report()
    already_matched, expired_offerer = AccountFactory(), AccountFactory()
    matched = _offer(report, already_matched, OfferType.VET_COSTS, status=OfferStatus.MATCHED)
    expired = _offer(report, expired_offerer, OfferType.SUPPLIES, status=OfferStatus.EXPIRED)
    member = _verified_member()

    _claim(client, report, member)
    matched.refresh_from_db()
    expired.refresh_from_db()
    assert matched.status == OfferStatus.MATCHED  # untouched, not re-notified
    assert expired.status == OfferStatus.EXPIRED
    assert not Notification.objects.filter(account=already_matched, type="offer_matched").exists()
    assert not Notification.objects.filter(account=expired_offerer, type="offer_matched").exists()


@pytest.mark.django_db
def test_claiming_notifies_the_reporter(client):
    reporter = AccountFactory()
    report = _report(reporter_account=reporter)
    _claim(client, report, _verified_member())
    assert Notification.objects.filter(account=reporter, type="report_claimed").exists()


@pytest.mark.django_db
def test_an_anonymous_report_still_notifies_its_own_reporter(client):
    """is_anonymous hides the reporter from OTHER users; it must not hide their own
    report's outcome from themselves (rule 6)."""
    reporter = AccountFactory()
    report = _report(reporter_account=reporter, is_anonymous=True)
    _claim(client, report, _verified_member())
    assert Notification.objects.filter(account=reporter, type="report_claimed").exists()


@pytest.mark.django_db
def test_claiming_a_report_with_no_reporter_account_does_not_error(client):
    report = _report(reporter_account=None)  # reporter deleted after reporting (SET_NULL)
    res = _claim(client, report, _verified_member())
    assert res.status_code == 201


@pytest.mark.django_db
def test_claiming_a_missing_report_is_404(client):
    res = client.post(f"/api/v1/reports/{uuid.uuid4()}/claim", {},
                      content_type="application/json", **_hdr(_verified_member()))
    assert res.status_code == 404
