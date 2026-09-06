"""US-Q2 follow-up · `deploy/cron.d/kupkop` is checked, not just written.

Two properties, both learned the hard way, both asserted against the REAL file rather than
against a list someone remembered to update:

1. **Every scheduled command refuses to run twice at once.**
   `common/management_base.py` claims a command added later "gets the guard by inheriting",
   which is only true if somebody notices to inherit. Add a cron line for an unguarded
   command and this file goes red — the only version of that claim worth making.

2. **Every nightly job actually runs at night, in MANILA.**
   The crontab is in UTC and the users are in UTC+8, and for three sprints both purge entries
   carried the comment "low-traffic window" while running at 10:00 and 10:30 PHT — mid-morning.
   The arithmetic in those comments was right; the conclusion was not, and a comment cannot
   catch that because a comment is what was wrong. Doing the conversion in a test is the only
   thing that can.
"""
import os
import re

import pytest
from django.core.management import load_command_class

from common.management_base import SingletonCommand

CRONTAB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "deploy", "cron.d", "kupkop")

# manage.py <command>, ignoring the leading schedule/user/path noise on a cron line.
INVOCATION = re.compile(r"manage\.py\s+([a-z_][a-z0-9_]*)")
# minute hour dom mon dow — enough to tell a daily job from an hourly one.
SCHEDULE = re.compile(r"^\s*(\S+)\s+(\S+)\s+\S+\s+\S+\s+\S+\s")

PH_UTC_OFFSET = 8
# The overnight trough for a Metro Manila userbase. Deliberately narrow: the point is to make
# a mid-morning slot impossible to write by accident, not to permit anything vaguely nocturnal.
QUIET_HOURS_PHT = range(1, 5)          # 01:00–04:59 PHT


def _job_lines():
    with open(CRONTAB) as fh:
        return [ln for ln in fh
                if ln.strip() and not ln.lstrip().startswith("#") and "manage.py" in ln]


def scheduled_commands():
    return sorted({m.group(1) for ln in _job_lines() for m in INVOCATION.finditer(ln)})


def nightly_jobs():
    """(command, utc_hour, utc_minute) for every job that runs once a day.

    An hourly job (`hour == "*"`) has no slot to get wrong and is skipped.
    """
    out = []
    for ln in _job_lines():
        sched = SCHEDULE.match(ln)
        cmd = INVOCATION.search(ln)
        if not (sched and cmd):
            continue
        minute, hour = sched.group(1), sched.group(2)
        if hour == "*" or not hour.isdigit():
            continue
        out.append((cmd.group(1), int(hour), int(minute)))
    return sorted(out)


def test_the_crontab_is_where_we_think_it_is():
    # If this file moves or is renamed, the parametrized test below would silently collect
    # ZERO cases and pass — a guard that guards nothing, which is the failure this whole
    # story is about.
    assert os.path.exists(CRONTAB), CRONTAB
    assert len(scheduled_commands()) >= 4, scheduled_commands()


@pytest.mark.parametrize("name", scheduled_commands())
def test_every_scheduled_command_refuses_to_run_twice_at_once(name):
    app = {"run_sweeps": "sagip", "run_matching_sweep": "sagip",
           "purge_deleted_accounts": "accounts",
           "purge_expired_documents": "verifications"}.get(name)
    assert app, (f"'{name}' is scheduled in deploy/cron.d/kupkop but this test does not know "
                 "which app owns it — add it here, and give it a SingletonCommand base.")
    cls = load_command_class(app, name)
    assert isinstance(cls, SingletonCommand), (
        f"'{name}' runs on a schedule but is not a SingletonCommand, so two ticks can "
        "overlap. Subclass common.management_base.SingletonCommand and rename handle() "
        "to run().")


def test_every_schedule_field_is_a_LEGAL_cron_field():
    """cron refuses to load a file containing an out-of-range field — and it refuses the
    WHOLE file, so one bad minute silently disables every job in it.

    Written after producing exactly that: an ordered string-replace over this crontab turned
    `30 2 * * *` into `350 18 * * *`, because an earlier replacement had already eaten the
    `0 2 * * *` substring inside it. The local-time test above passed anyway — it read `350`
    as the minute and `18` as the hour, which converts to a perfectly respectable 02:20 PHT.
    Parsing a field is not the same as validating it.
    """
    for cmd, hour, minute in nightly_jobs():
        assert 0 <= hour <= 23, f"{cmd}: hour {hour} is not 0-23"
        assert 0 <= minute <= 59, f"{cmd}: minute {minute} is not 0-59"


def test_there_are_nightly_jobs_to_check():
    # Without this, a parsing change that matched nothing would leave the parametrized test
    # below collecting zero cases and passing — the exact shape of failure this file exists
    # to prevent.
    assert len(nightly_jobs()) >= 3, nightly_jobs()


@pytest.mark.parametrize("cmd,hour,minute", nightly_jobs())
def test_every_nightly_job_runs_overnight_in_manila(cmd, hour, minute):
    """The crontab is UTC; the users are UTC+8. Assert the LOCAL time, not the written one.

    This is the test that would have caught three sprints of `purge_*` entries commented
    "low-traffic window" and scheduled for 10:00 PHT. A reviewer reads "0 2 * * *" next to
    "low-traffic window" and agrees, because both look right in isolation.
    """
    pht = (hour + PH_UTC_OFFSET) % 24
    assert pht in QUIET_HOURS_PHT, (
        f"'{cmd}' runs at {hour:02d}:{minute:02d} UTC = {pht:02d}:{minute:02d} PHT, outside "
        f"the {QUIET_HOURS_PHT.start:02d}:00-{QUIET_HOURS_PHT.stop - 1:02d}:59 PHT quiet "
        "window. The crontab is UTC and the userbase is UTC+8 — check the LOCAL hour.")


def test_nightly_jobs_do_not_start_in_the_same_minute():
    """They share one small database, and the matching sweep alone runs for ~11.5 minutes at
    10x projected volume (US-Q2). Advisory locks keep a job from overlapping ITSELF; nothing
    stops three different jobs from piling onto the same instant."""
    starts = [(h, m) for _, h, m in nightly_jobs()]
    assert len(starts) == len(set(starts)), nightly_jobs()
