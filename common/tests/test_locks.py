"""US-Q2 follow-up · the cron overlap guard.

US-Q2 measured `sweep_matches` at 11.5 minutes on 50,000 reports — 37x the time for 10x the
data. It fits an hourly cron today with roughly 5x of margin, and the next 10x of growth
does not: at that point two `run_sweeps` processes are running the same work at the same
time, every hour, forever.

⚠️ WHY AN ADVISORY LOCK AND NOT `flock` ON THE CRON LINE. §16.1 runs the backend as 1-2
Fargate tasks. `flock` is a file lock on one host's filesystem, so the day a second task
exists it guards nothing while looking exactly as if it does. The lock has to live where
both hosts can see it, and the database is the only such place this stack has.

What overlapping runs actually cost, so the guard is sized against a real risk rather than
a tidy one: `run_matching` is idempotent by design and `report_match` carries
UNIQUE(report, matched_report), so a duplicate pass does NOT double-notify. It burns a
second CPU and a second connection pool doing work already in flight, against the same
database serving requests. That is a capacity problem, not a correctness one — which is
why this is a guard and not a lock held across the whole transaction.
"""
import os
import subprocess
import sys

import psycopg
import pytest
from django.db import connection

from common.locks import LockBusy, advisory_lock, lock_key


def _rival():
    """A SECOND, independent connection to the same database.

    Advisory locks are held per SESSION, so a test that acquires twice on Django's own
    connection would just re-enter its own lock and pass while proving nothing. Contention
    only exists between connections, so the test has to open one.
    """
    d = connection.settings_dict
    return psycopg.connect(dbname=d["NAME"], host=d.get("HOST") or None,
                           user=d.get("USER") or None, password=d.get("PASSWORD") or None)


@pytest.mark.django_db
def test_the_body_runs_when_the_lock_is_free():
    ran = []
    with advisory_lock("test-free"):
        ran.append(True)
    assert ran == [True]


@pytest.mark.django_db
def test_a_second_holder_is_refused_rather_than_queued():
    # THE test. It must not block: a cron job that waits for the previous run turns an
    # overlap into a pile-up, which is the failure this guard exists to prevent.
    rival = _rival()
    try:
        with rival.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", [lock_key("test-busy")])
        with pytest.raises(LockBusy):
            with advisory_lock("test-busy"):
                pytest.fail("the body must not run while another session holds the lock")
    finally:
        rival.close()


@pytest.mark.django_db
def test_the_lock_is_released_when_the_body_finishes():
    with advisory_lock("test-release"):
        pass
    with advisory_lock("test-release"):      # would raise LockBusy if it leaked
        pass


@pytest.mark.django_db
def test_the_lock_is_released_when_the_body_RAISES():
    # A sweep that dies holding the lock would silently disable itself until the next
    # process restart — the guard would become the outage.
    with pytest.raises(ValueError):
        with advisory_lock("test-boom"):
            raise ValueError("boom")
    with advisory_lock("test-boom"):
        pass


@pytest.mark.django_db
def test_different_names_do_not_block_each_other():
    # run_sweeps and purge_deleted_accounts share a machine and must not serialize behind
    # one another — they are unrelated jobs on different schedules.
    with advisory_lock("job-a"):
        with advisory_lock("job-b"):
            pass


def test_the_key_fits_a_signed_bigint():
    # Postgres advisory locks are keyed on bigint; anything wider is an error at the call.
    assert lock_key("run_sweeps") != lock_key("purge_deleted_accounts")
    for name in ["run_sweeps", "purge_deleted_accounts", "", "x" * 500]:
        assert -(2**63) <= lock_key(name) < 2**63


def test_the_key_is_stable_ACROSS_PROCESSES():
    """The one property that makes this a lock at all — and it needs a subprocess to test.

    Python salts string hashing per interpreter (PYTHONHASHSEED), so `hash(name)` would give
    two Fargate tasks DIFFERENT keys for the same job: they would take two different locks,
    never contend, and overlap exactly as before behind a guard that looks present. An
    in-process assertion cannot catch this — `hash()` is perfectly stable *within* one
    process, so a same-process test passes on the broken implementation. (Verified: swapping
    the digest for `hash()` leaves every other test in this file green.)
    """
    src = ("import sys; sys.path.insert(0, %r);"
           "import hashlib;"
           "d=hashlib.blake2b(b'run_sweeps', digest_size=8).digest();"
           "print(int.from_bytes(d,'big',signed=True))")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    keys = set()
    for seed in ("0", "1", "random"):
        out = subprocess.run([sys.executable, "-c", src % root],
                             env={**os.environ, "PYTHONHASHSEED": seed},
                             capture_output=True, text=True, check=True)
        keys.add(int(out.stdout.strip()))
    assert len(keys) == 1, f"key differs between interpreters: {keys}"
    assert keys.pop() == lock_key("run_sweeps")
