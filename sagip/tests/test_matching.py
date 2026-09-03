"""US-L2 · lost<->found matching (§11)."""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from notifications.models import Notification
from sagip.matching import run_matching, sweep_matches
from sagip.models import MatchStatus, ReportMatch, ReportType, StrayReport, StrayStatus


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _report(rtype, *, species="dog", lat=14.650, lng=121.100, breed=None, color=None,
            size=None, sex=None, reporter=None, status=StrayStatus.REPORTED, days_ago=0,
            notes=""):
    r = StrayReport.objects.create(
        report_type=rtype, species=species, condition="healthy",
        geom=Point(lng, lat, srid=4326), reporter_account=reporter or AccountFactory(),
        breed=breed, color_markings=color, size_category=size, sex=sex, notes=notes,
        status=status)
    if days_ago:
        when = timezone.now() - timezone.timedelta(days=days_ago)
        StrayReport.objects.filter(pk=r.pk).update(created_at=when)
        r.refresh_from_db()
    return r


@pytest.mark.django_db
def test_a_nearby_opposite_type_same_species_report_matches():
    lost = _report(ReportType.LOST, breed="aspin", color="brown white", size="medium", sex="male")
    found = _report(ReportType.FOUND, breed="aspin", color="brown", size="medium", sex="male",
                    lat=14.651, lng=121.101)
    matches = run_matching(found)
    assert len(matches) == 1
    m = matches[0]
    assert m.matched_report_id == lost.pk and m.status == MatchStatus.SUGGESTED
    assert m.signals["geo"] > 0.9 and m.signals["size_sex"] == 1.0


@pytest.mark.django_db
def test_same_type_does_not_match():
    _report(ReportType.LOST)
    other_lost = _report(ReportType.LOST, lat=14.651, lng=121.101)
    assert run_matching(other_lost) == []


@pytest.mark.django_db
def test_different_species_does_not_match():
    _report(ReportType.LOST, species="cat")
    found = _report(ReportType.FOUND, species="dog", lat=14.651, lng=121.101)
    assert run_matching(found) == []


@pytest.mark.django_db
def test_far_apart_does_not_match():
    _report(ReportType.LOST, lat=14.65, lng=121.10)
    found = _report(ReportType.FOUND, lat=15.50, lng=122.00)   # ~130 km away
    assert run_matching(found) == []


@pytest.mark.django_db
def test_outside_the_time_window_does_not_match():
    _report(ReportType.LOST, lat=14.650, lng=121.100, days_ago=60)
    found = _report(ReportType.FOUND, lat=14.651, lng=121.101)
    assert run_matching(found) == []


@pytest.mark.django_db
def test_a_resolved_report_is_not_a_candidate():
    _report(ReportType.LOST, status=StrayStatus.RESOLVED, lat=14.650, lng=121.100)
    found = _report(ReportType.FOUND, lat=14.651, lng=121.101)
    assert run_matching(found) == []


@pytest.mark.django_db
def test_a_weak_pair_below_threshold_is_not_persisted():
    # ~9 km apart, 27 days apart, no describables -> ~0.055, below 0.45.
    _report(ReportType.LOST, lat=14.650, lng=121.100, days_ago=27)
    found = _report(ReportType.FOUND, lat=14.730, lng=121.100)
    assert run_matching(found) == []


@pytest.mark.django_db
def test_matching_is_idempotent_and_notifies_once():
    lost = _report(ReportType.LOST, breed="aspin", lat=14.650, lng=121.100)
    found = _report(ReportType.FOUND, breed="aspin", lat=14.651, lng=121.101)
    run_matching(found)
    run_matching(found)   # re-run: refreshes score, must not duplicate or re-notify
    assert ReportMatch.objects.filter(report=found, matched_report=lost).count() == 1
    # both reporters notified, exactly once each
    assert Notification.objects.filter(account=found.reporter_account,
                                       type="match_suggested").count() == 1
    assert Notification.objects.filter(account=lost.reporter_account,
                                       type="match_suggested").count() == 1


@pytest.mark.django_db
def test_only_top_five_are_kept():
    for i in range(7):
        _report(ReportType.LOST, breed="aspin", lat=14.650 + i * 0.001, lng=121.100)
    found = _report(ReportType.FOUND, breed="aspin", lat=14.650, lng=121.100)
    assert len(run_matching(found)) == 5


@pytest.mark.django_db
def test_get_matches_returns_signals_for_the_reporter(client):
    reporter = AccountFactory()
    _report(ReportType.LOST, breed="aspin", lat=14.650, lng=121.100)
    found = _report(ReportType.FOUND, breed="aspin", lat=14.651, lng=121.101, reporter=reporter)
    run_matching(found)
    res = client.get(f"/api/v1/reports/{found.pk}/matches", **_hdr(reporter))
    assert res.status_code == 200
    results = res.json()["results"]
    assert len(results) == 1 and results[0]["signals"] is not None


@pytest.mark.django_db
def test_a_non_reporter_cannot_read_matches(client):
    found = _report(ReportType.FOUND, lat=14.651, lng=121.101)
    res = client.get(f"/api/v1/reports/{found.pk}/matches", **_hdr(AccountFactory()))
    assert res.status_code == 403


@pytest.mark.django_db
def test_confirm_resolves_both_reports_and_a_second_decision_is_409(client):
    a, b = AccountFactory(), AccountFactory()
    lost = _report(ReportType.LOST, breed="aspin", lat=14.650, lng=121.100, reporter=a)
    found = _report(ReportType.FOUND, breed="aspin", lat=14.651, lng=121.101, reporter=b)
    m = run_matching(found)[0]
    res = client.post(f"/api/v1/reports/{found.pk}/matches/{m.pk}/confirm", **_hdr(b))
    assert res.status_code == 200 and res.json()["status"] == MatchStatus.CONFIRMED
    lost.refresh_from_db(); found.refresh_from_db()
    assert lost.status == StrayStatus.RESOLVED and found.status == StrayStatus.RESOLVED
    # the other reporter trying to decide the same match now -> 409
    res2 = client.post(f"/api/v1/reports/{lost.pk}/matches/{m.pk}/dismiss", **_hdr(a))
    assert res2.status_code == 409 and res2.json()["error"]["code"] == "match_decided"


@pytest.mark.django_db
def test_dismiss_hides_the_match_without_resolving(client):
    a, b = AccountFactory(), AccountFactory()
    lost = _report(ReportType.LOST, breed="aspin", lat=14.650, lng=121.100, reporter=a)
    found = _report(ReportType.FOUND, breed="aspin", lat=14.651, lng=121.101, reporter=b)
    m = run_matching(found)[0]
    res = client.post(f"/api/v1/reports/{found.pk}/matches/{m.pk}/dismiss", **_hdr(a))
    assert res.status_code == 200
    found.refresh_from_db()
    assert found.status == StrayStatus.REPORTED           # not resolved
    # dismissed matches drop off the reporter's list
    assert client.get(f"/api/v1/reports/{found.pk}/matches",
                      **_hdr(b)).json()["results"] == []


@pytest.mark.django_db
def test_the_sweep_matches_open_reports():
    _report(ReportType.LOST, breed="aspin", lat=14.650, lng=121.100)
    _report(ReportType.FOUND, breed="aspin", lat=14.651, lng=121.101)
    assert len(sweep_matches()) >= 1
