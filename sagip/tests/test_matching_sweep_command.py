"""US-Q2 follow-up · the L&F matching sweep moved to its OWN nightly command.

§11.4 defines two triggers and only one of them is this sweep:

    "On create / edit of a lost or found report (synchronous candidate scan)"
    "Nightly sweep as a safety net for near-miss windows and newly-added detail."

The synchronous scan is `sagip/views.py`'s `run_matching(report)` on create, and it is what
a user's reunion actually depends on. `sweep_matches` is the *safety net* — it re-scores
still-open reports so a pair that was a near miss yesterday (before someone added a breed,
or before the second report existed) gets another look.

It had been folded into `run_sweeps`, which cron runs HOURLY, so the safety net was running
24x more often than §11.4 asks. US-Q2 measured it at 11.5 minutes over 50,000 reports —
37x the time for 10x the data — which turns that into 11.5 minutes of database load every
hour for a job the spec wants once a night.

⚠️ The first test here is the one that makes this change safe: **moving the sweep must not
delay anybody's match.** If creation-time matching did not exist, going hourly → nightly
would mean a lost dog waits up to a day for its candidate list.
"""
import pytest
from django.contrib.gis.geos import Point
from django.core.management import call_command

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from sagip.models import MatchStatus, ReportMatch, ReportType, StrayReport, StrayStatus


def _report(rtype, *, lat=14.650, lng=121.100):
    return StrayReport.objects.create(
        report_type=rtype, species="dog", condition="healthy",
        geom=Point(lng, lat, srid=4326), reporter_account=AccountFactory(),
        breed="aspin", color_markings="brown white", size_category="medium", sex="male",
        status=StrayStatus.REPORTED)


@pytest.mark.django_db
def test_filing_a_report_still_matches_IMMEDIATELY(client):
    """The safety net's cadence is irrelevant to a real reunion — this is why.

    §11.4's synchronous scan runs on create. If this ever regresses, moving the sweep to
    nightly silently becomes "your lost dog gets candidates tomorrow."
    """
    _report(ReportType.FOUND)
    reporter = AccountFactory()
    res = client.post("/api/v1/reports", {
        "report_type": "lost", "species": "dog", "condition": "healthy",
        "lat": 14.651, "lng": 121.101, "breed": "aspin",
        "color_markings": "brown white", "size_category": "medium", "sex": "male",
        "location_text": "Marikina"},
        content_type="application/json",
        **{"HTTP_AUTHORIZATION": f"Bearer {tokens_for(reporter)['access']}"})
    assert res.status_code == 201, res.content
    assert ReportMatch.objects.filter(status=MatchStatus.SUGGESTED).exists()


@pytest.mark.django_db
def test_run_sweeps_no_longer_does_the_matching_sweep(capsys):
    _report(ReportType.LOST)
    _report(ReportType.FOUND, lat=14.651, lng=121.101)

    call_command("run_sweeps")
    out = capsys.readouterr().out
    assert "matched" not in out
    assert not ReportMatch.objects.exists(), (
        "run_sweeps is hourly; §11.4 asks for this safety net nightly")


@pytest.mark.django_db
def test_the_nightly_command_does_the_matching_sweep(capsys):
    _report(ReportType.LOST)
    _report(ReportType.FOUND, lat=14.651, lng=121.101)

    call_command("run_matching_sweep")
    assert "matched" in capsys.readouterr().out
    assert ReportMatch.objects.filter(status=MatchStatus.SUGGESTED).exists()


@pytest.mark.django_db
def test_the_hourly_sweeps_still_run(capsys):
    # The welfare-time-sensitive ones — escalation, stalled claims, offer expiry, shift
    # reminders — must stay hourly. Splitting the wrong sweep out would delay an escalation.
    call_command("run_sweeps")
    out = capsys.readouterr().out
    for label in ("escalated", "expired", "reminded", "badged", "offer"):
        assert label in out, out
