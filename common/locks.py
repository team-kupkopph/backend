"""US-Q2 follow-up · a cross-host mutex for the scheduled commands.

US-Q2 measured `sweep_matches` at 11.5 minutes over 50,000 reports — 37x the time for 10x
the data (≈O(n^1.57)). Today that fits its hourly cron with about 5x of margin. The next
10x of growth does not, and the failure mode is not "the sweep is slow": it is two
`run_sweeps` processes doing the same work simultaneously, every hour, indefinitely.

WHY AN ADVISORY LOCK AND NOT `flock` ON THE CRON LINE
    §16.1 runs the backend as 1-2 Fargate tasks. `flock` takes a file lock on ONE host's
    filesystem, so the day a second task exists it guards nothing — while looking, in the
    crontab, exactly as though it does. A guard that silently stops guarding is worse than
    no guard, because nobody re-checks it. The lock has to live somewhere both hosts can
    see, and the database is the only such place in this stack.

WHY *TRY* AND NOT WAIT
    `pg_advisory_lock` blocks until the holder releases. For an hourly job that would turn
    an overlap into a queue: each run waits for the last, and the backlog only grows. The
    correct behaviour for a periodic job is to notice that the previous run is still going
    and **skip this tick** — the work is not lost, it happens on the next one.

WHAT AN OVERLAP ACTUALLY COSTS, stated so the guard is sized to the real risk: `run_matching`
is idempotent and `report_match` carries UNIQUE(report, matched_report), so a duplicate pass
does not double-notify anyone. It burns a second CPU and a second connection pool redoing
work already in flight, against the same database that is serving requests. A capacity
problem, not a correctness one — which is why this is a coarse per-command guard and not a
lock held across application transactions.
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager

from django.db import connection

logger = logging.getLogger("kupkop.locks")


class LockBusy(RuntimeError):
    """Another session holds this lock — the previous run has not finished."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"another process already holds the '{name}' lock")


def lock_key(name: str) -> int:
    """A stable signed-bigint key for `name`.

    ⚠️ NOT `hash()`. Python salts string hashing per process (PYTHONHASHSEED), so two hosts
    — or the same host on two days — would derive DIFFERENT keys from the same name, never
    contend, and the guard would be a no-op that passes every test on one machine. A digest
    is stable across processes, hosts and releases, which is the entire requirement.
    """
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def advisory_lock(name: str):
    """Hold a cluster-wide lock for the duration of the block, or raise `LockBusy`.

    Session-scoped (`pg_try_advisory_lock`, not the `_xact_` variant) because these jobs run
    many transactions and must stay guarded across all of them. Session scope means the lock
    MUST be released explicitly, hence the `finally` — a command that died holding it would
    disable itself until the process exited, and the guard would become the outage.
    """
    key = lock_key(name)
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", [key])
        if not cur.fetchone()[0]:
            raise LockBusy(name)
    try:
        yield
    finally:
        # Best-effort: if the connection is already gone, so is the lock — Postgres frees
        # session locks when the session ends. Never mask the original exception with a
        # cleanup failure.
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", [key])
        except Exception:                                   # noqa: BLE001
            logger.warning({"event": "advisory_unlock_failed", "lock": name})
