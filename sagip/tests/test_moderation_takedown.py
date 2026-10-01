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
from sagip.models import (
    MatchStatus,
    OfferStatus,
    ReportMatch,
    ReportOffer,
    RescueCase,
    StrayReport,
)
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


def _claimed(report=None):
    report = report or _report()
    claimer = _verified()
    res = _c(claimer).post(f"/api/v1/reports/{report.pk}/claim")
    assert res.status_code == 201
    report.refresh_from_db()
    return report, RescueCase.objects.get(pk=res.json()["case_id"]), claimer


def _offer(report):
    return ReportOffer.objects.create(report=report, account=AccountFactory(), offer_type="transport",
                                      status=OfferStatus.OPEN,
                                      expires_at=timezone.now() + timezone.timedelta(hours=40))


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
    offered = _report()
    offer = _offer(offered)
    report, case, claimer = _claimed(offered)
    offer.refresh_from_db()
    assert offer.status == OfferStatus.MATCHED      # the claim matched it
    _action(report)
    case.refresh_from_db(); offer.refresh_from_db()
    assert case.expired_at is not None
    assert offer.status == OfferStatus.EXPIRED      # C13 · not stranded MATCHED on a dead claim
    [n] = Notification.objects.filter(account=claimer, type="report_removed")
    assert n.data == {"report_id": str(report.pk)}

    custody = _report()
    kept_offer = _offer(custody)
    kept, kept_case, keeper = _claimed(custody)
    assert _c(keeper).post(f"/api/v1/cases/{kept_case.pk}/status", {"status": "rescued"},
                           format="json").status_code == 200
    _action(kept)
    kept_case.refresh_from_db(); kept.refresh_from_db()
    assert kept_case.expired_at is None and kept.status == "rescued"
    assert _c(keeper).get(f"/api/v1/reports/{kept.pk}").status_code == 200   # active claimer still sees it
    kept_offer.refresh_from_db()
    assert kept_offer.status == OfferStatus.MATCHED   # the rescue it serves is still going


@pytest.mark.django_db
def test_a_claim_that_commits_while_the_takedown_waits_still_ends(monkeypatch):
    """C13 · hide_report's first case lookup can't see a case whose claim hasn't committed, so it
    finds nothing; it then wakes on the report lock to a claimed report with no case in hand.
    Simulated by making that first lookup see nothing while the claimed case exists."""
    from sagip.moderation import hide_report
    report, case, claimer = _claimed()
    real = RescueCase.objects.select_for_update
    calls = []

    def first_lookup_misses(*a, **kw):
        calls.append(1)
        qs = real(*a, **kw)
        return qs.none() if len(calls) == 1 else qs
    monkeypatch.setattr(RescueCase.objects, "select_for_update", first_lookup_misses)

    hide_report(report, AccountFactory())
    case.refresh_from_db()
    assert len(calls) == 2                     # the miss, then the re-query under the report lock
    assert case.expired_at is not None
    assert Notification.objects.filter(account=claimer, type="report_removed").count() == 1


def _pair(**match_kw):
    """A lost report and a found report about it, paired by the matcher. The found side's
    reporter consented to share contact, so a leak would carry their phone and email."""
    lost = _report(report_type="lost", condition="healthy")
    found = _report(report_type="found", condition="healthy", contact_share_consent=True)
    match = ReportMatch.objects.create(report=found, matched_report=lost, score="0.900",
                                       **match_kw)
    return lost, found, match


def _hide(report):
    """Set hidden_at without running hide_report, so the views' own guard is what's tested."""
    StrayReport.objects.filter(pk=report.pk).update(hidden_at=timezone.now())


@pytest.mark.django_db
@pytest.mark.parametrize("status", [MatchStatus.SUGGESTED, MatchStatus.CONFIRMED])
def test_a_hidden_report_drops_out_of_both_sides_match_lists(status):
    """C13 · the hidden report's reporter can't see the other side's contact, and the other
    side no longer sees the hidden report or who filed it."""
    lost, found, _ = _pair(status=status)
    _hide(found)
    for side in (found, lost):
        res = _c(side.reporter_account).get(f"/api/v1/reports/{side.pk}/matches")
        assert res.status_code == 200
        assert res.json()["results"] == []


@pytest.mark.django_db
@pytest.mark.parametrize("hidden_side", ["found", "lost"])
def test_a_match_with_a_hidden_side_cannot_be_decided(hidden_side):
    """C13 · confirming would resolve the other reporter's real report off a removed one."""
    lost, found, match = _pair()
    _hide(found if hidden_side == "found" else lost)
    for side in (found, lost):
        res = _c(side.reporter_account).post(
            f"/api/v1/reports/{side.pk}/matches/{match.pk}/confirm")
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "not_found"
    match.refresh_from_db(); lost.refresh_from_db()
    assert match.status == MatchStatus.SUGGESTED and lost.status == "reported"


@pytest.mark.django_db
def test_a_takedown_dismisses_the_reports_suggested_matches_either_side():
    lost, found, as_found = _pair()
    other_found = _report(report_type="found", condition="healthy")
    as_lost = ReportMatch.objects.create(report=other_found, matched_report=found, score="0.5")
    confirmed = ReportMatch.objects.create(report=found, matched_report=_report(report_type="lost"),
                                           score="0.5", status=MatchStatus.CONFIRMED)
    untouched = ReportMatch.objects.create(report=other_found, matched_report=lost, score="0.5")
    _action(found)
    for m in (as_found, as_lost, confirmed, untouched):
        m.refresh_from_db()
    assert as_found.status == MatchStatus.DISMISSED and as_lost.status == MatchStatus.DISMISSED
    assert confirmed.status == MatchStatus.CONFIRMED      # a decided match is history, not a lead
    assert untouched.status == MatchStatus.SUGGESTED


@pytest.mark.django_db
@pytest.mark.parametrize("gone", ["hidden", "deleted"])
def test_an_offer_racing_a_takedown_or_a_delete_gets_404(monkeypatch, gone):
    """C13 · the report vanishes between the offer view's unlocked check and its locked
    re-read (the serializer runs in between, so that's where it's made to happen)."""
    from sagip import views
    report = _report()
    real = views.OfferCreateSerializer.is_valid

    def vanish_then_validate(self, *a, **kw):
        if gone == "hidden":
            StrayReport.objects.filter(pk=report.pk).update(hidden_at=timezone.now())
        else:
            StrayReport.objects.filter(pk=report.pk).delete()
        return real(self, *a, **kw)
    monkeypatch.setattr(views.OfferCreateSerializer, "is_valid", vanish_then_validate)

    res = _c(AccountFactory()).post(f"/api/v1/reports/{report.pk}/offers",
                                    {"offer_type": "transport"}, format="json")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"
    assert not ReportOffer.objects.filter(report_id=report.pk).exists()


@pytest.mark.django_db
def test_the_reporters_own_list_says_which_reports_were_removed():
    """C13 / D10 · the reporter sees "Removed by moderation" in their own list."""
    reporter = AccountFactory()
    kept, removed = _report(reporter_account=reporter), _report(reporter_account=reporter)
    _action(removed)
    rows = {r["report_id"]: r for r in _c(reporter).get("/api/v1/me/reports").json()["results"]}
    assert rows[str(removed.pk)]["hidden"] is True
    assert rows[str(kept.pk)]["hidden"] is False
