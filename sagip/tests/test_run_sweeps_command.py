"""US-F0 — the `run_sweeps` management command wires both sweeps together and is
runnable from cron/dev without a scheduler in the loop."""
import psycopg
import pytest
from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from common.locks import lock_key
from sagip.models import StrayReport


def _rival_connection():
    """A second session, so the lock has something to contend with (see common/locks.py)."""
    d = connection.settings_dict
    return psycopg.connect(dbname=d["NAME"], host=d.get("HOST") or None,
                           user=d.get("USER") or None, password=d.get("PASSWORD") or None)


@pytest.mark.django_db
def test_run_sweeps_reports_what_it_did(capsys):
    StrayReport.objects.create(
        species="dog", condition="injured", status="reported",
        geom=Point(121.05, 14.63, srid=4326))
    StrayReport.objects.filter().update(
        created_at=timezone.now() - timezone.timedelta(hours=5))

    call_command("run_sweeps")
    out = capsys.readouterr().out
    assert "escalated" in out and "expired" in out and "offer" in out


# ── US-Q2 follow-up · the overlap guard ─────────────────────────────────────────────
@pytest.mark.django_db
def test_run_sweeps_skips_when_a_previous_run_is_still_going(capsys):
    """An hourly cron whose job now takes 11.5 minutes at 10x volume must SKIP, not queue.

    US-Q2 measured sweep_matches at 37x the time for 10x the data. The guard's job is to
    make the second tick a no-op rather than a second copy of the same work against the
    same database.
    """
    rival = _rival_connection()
    try:
        with rival.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", [lock_key("run_sweeps")])
        call_command("run_sweeps")
        out = capsys.readouterr().out
        assert "already running" in out
        assert "escalated" not in out          # no sweep ran
    finally:
        rival.close()


@pytest.mark.django_db
def test_a_skipped_run_exits_zero(capsys):
    """A skip is normal operation, not a failure.

    cron mails on non-zero exit and an alerting pipeline pages on it. A job that reports
    "the previous run is still going" as an error trains everyone to mute it — and the mute
    outlives the condition.
    """
    rival = _rival_connection()
    try:
        with rival.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", [lock_key("run_sweeps")])
        call_command("run_sweeps")            # SystemExit / CommandError would fail here
    finally:
        rival.close()


@pytest.mark.django_db
def test_the_lock_is_released_so_the_next_tick_runs(capsys):
    call_command("run_sweeps")
    capsys.readouterr()
    call_command("run_sweeps")
    assert "escalated" in capsys.readouterr().out


@pytest.mark.django_db
def test_the_purge_commands_are_guarded_separately_from_the_sweeps(capsys):
    """Different jobs, different schedules, different locks.

    Serializing the irreversible RA 10173 purge behind an unrelated hourly sweep would let
    sweep cadence silently govern how fast personal data is destroyed — the exact coupling
    US-N2 gave the purge its own cron entry to avoid.
    """
    rival = _rival_connection()
    try:
        with rival.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", [lock_key("run_sweeps")])
        call_command("purge_deleted_accounts")
        assert "anonymized" in capsys.readouterr().out
    finally:
        rival.close()


def test_the_help_names_placement_expiry():
    # C14 / D11 · `manage.py help run_sweeps` is how an operator learns what the cron line does.
    from sagip.management.commands.run_sweeps import Command
    assert "placement" in Command.help.lower()


# ── R2 · one failing sweep must not skip the others ─────────────────────────────────
def _stubs(monkeypatch, failing=(), offers_fails=False):
    """Replace SWEEPS with recording stubs; `failing` names the ones that raise."""
    from sagip.management.commands import run_sweeps as mod
    calls = []

    def make(label):
        def fn():
            calls.append(label)
            if label in failing:
                raise RuntimeError(f"{label} boom")
            return [object()]
        return fn

    monkeypatch.setattr(mod, "SWEEPS", [(lb, make(lb)) for lb in ("a", "b", "c")])

    def offers():
        calls.append("offers")
        if offers_fails:
            raise RuntimeError("offers boom")
        return 2
    monkeypatch.setattr(mod, "expire_offers", offers)
    return calls


@pytest.mark.django_db
def test_a_failing_sweep_does_not_skip_the_others(monkeypatch, capsys, caplog):
    # R2 · before this, one raising sweep aborted the command and every later sweep (offer
    # expiry included) silently did not run until the next tick — or ever, if it kept failing.
    calls = _stubs(monkeypatch, failing={"b"})
    with caplog.at_level("ERROR", logger="kupkop.sweeps"):
        call_command("run_sweeps")                      # exit 0: not every sweep failed
    out = capsys.readouterr().out
    assert calls == ["a", "b", "c", "offers"]           # the rest still ran
    assert "b error" in out and "a 1" in out and "c 1" in out and "2 offer(s)" in out
    rec = [r for r in caplog.records if r.name == "kupkop.sweeps"]
    assert len(rec) == 1 and rec[0].exc_info and "sweep b failed" in rec[0].getMessage()


@pytest.mark.django_db
def test_a_failing_offer_sweep_is_isolated_too(monkeypatch, capsys, caplog):
    calls = _stubs(monkeypatch, offers_fails=True)
    with caplog.at_level("ERROR", logger="kupkop.sweeps"):
        call_command("run_sweeps")
    out = capsys.readouterr().out
    assert calls == ["a", "b", "c", "offers"]
    assert "offers error" in out and "a 1" in out
    assert any("sweep offers failed" in r.getMessage() for r in caplog.records)


@pytest.mark.django_db
def test_a_partial_failure_is_flagged_but_exits_zero(monkeypatch):
    # Warning styling for a partial failure, success styling otherwise; both exit 0.
    from sagip.management.commands.run_sweeps import Command
    _stubs(monkeypatch, failing={"a"})
    seen = {}
    cmd = Command()
    monkeypatch.setattr(cmd.style, "WARNING", lambda s: seen.setdefault("warn", s) and s)
    monkeypatch.setattr(cmd.style, "SUCCESS", lambda s: seen.setdefault("ok", s) and s)
    cmd.run()
    assert "a error" in seen["warn"] and "ok" not in seen
    seen.clear()
    _stubs(monkeypatch)
    cmd.run()
    assert "ok" in seen and "warn" not in seen


@pytest.mark.django_db
def test_every_sweep_failing_exits_non_zero(monkeypatch, capsys):
    from django.core.management.base import CommandError
    calls = _stubs(monkeypatch, failing={"a", "b", "c"}, offers_fails=True)
    with pytest.raises(CommandError) as exc:
        call_command("run_sweeps")
    assert calls == ["a", "b", "c", "offers"]           # all were still attempted
    assert "a error" in str(exc.value) and "offers error" in str(exc.value)
