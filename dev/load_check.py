#!/usr/bin/env python3
"""US-Q2 · the load check for §13.1's latency budgets.

    DATABASE_URL=postgres://localhost/kupkop_load \
    DJANGO_SECRET_KEY=... DJANGO_ALLOWED_HOSTS=testserver \
    python dev/load_check.py --seed --scale year1
    ... --measure --scale year1

⚠️ WHERE THIS RUNS, STATED UP FRONT. There is no staging environment (US-D3's standing
finding). This runs against local Postgres on a developer laptop, with production-SHAPED
data — right tables, right cardinality, right spatial distribution — but not production
hardware, not production concurrency, and not across a network. A number labelled
"staging" that came from a laptop is worse than no number, so this script labels itself.

WHAT IS AND IS NOT MEASURED
  Measured: URL routing, middleware, DRF, permissions, serialization, and every SQL
    round trip — i.e. the part of the latency budget the code controls, on a warm cache.
  Not measured: TLS, the ALB, the network, gunicorn's worker model, connection-pool
    contention, and any concurrency at all. Requests here are issued sequentially, so
    these are best-case latencies. Real p95 under load will be HIGHER, never lower.
  Consequence: a scenario that passes here is not proven to pass in production, but a
    scenario that FAILS here fails in production too. Treat green as "not disproven"
    and red as conclusive.

Settings run with DEBUG OFF on purpose. Under DEBUG, Django accumulates every query in
`connection.queries` for the life of the process — which both distorts the timings and
turns a 50k-row seed into a memory problem. The seed step turns it on deliberately for
its own query capture and says so.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import random
import sys
import time
import uuid
from datetime import timedelta

# A stress run spends many minutes inside one sweep. Redirected to a file, Python block-
# buffers stdout, so the log stays EMPTY the whole time and the run is indistinguishable
# from a hang — which is how the first stress run was nearly killed for being stuck.
sys.stdout.reconfigure(line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.contrib.gis.geos import Point  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402

from accounts.models import Account, AccountStatus  # noqa: E402
from accounts.tokens import tokens_for  # noqa: E402
from community.models import StoryPost  # noqa: E402
from listings.models import AdoptionListing  # noqa: E402
from sagip.geo import CITY_CENTROIDS  # noqa: E402
from sagip.models import ReportType, StrayReport, StrayStatus  # noqa: E402
from verifications.models import AccountCapability  # noqa: E402

# Two scales, because one number tells you where you are and two tell you which way the
# curve bends. `year1` is a plausible Metro Manila first year; `stress` is 10x it, so a
# scenario that is linear and a scenario that is quadratic separate visibly instead of
# both looking "fine" at a single size.
SCALES = {
    "year1":  {"accounts": 3_000,  "reports": 5_000,  "listings": 2_000, "stories": 1_000},
    "stress": {"accounts": 20_000, "reports": 50_000, "listings": 20_000, "stories": 10_000},
}

# §13.1, verbatim. `None` means the spec sets no target for it — reported, never failed.
TARGETS_MS = {
    "read": 300,     # "API read latency (p95) < 300 ms (simple reads)"
    "geo": 500,      # "< 500 ms (geo queries)" / "Map / nearby query < 500 ms within 10 km"
    "write": 600,    # "API write latency (p95) < 600 ms"
    "batch": None,   # sweeps are cron work; §13.1 budgets request latency, not batch jobs
}

MM_LAT, MM_LNG = 14.5995, 120.9842      # Manila, the densest seeded centre
SPECIES = ["dog", "cat"]
CONDITIONS = ["injured", "sick", "healthy"]


# ── seeding ─────────────────────────────────────────────────────────────────────────
def _scatter(rng, radius_km=25):
    """A point drawn inside `radius_km` of Manila, weighted toward the centre.

    sqrt() on the radius gives a uniform-by-AREA disc; without it every point crowds the
    middle and `ST_DWithin` gets an unrealistically easy job — the seed would flatter the
    very query this script exists to stress.
    """
    r = radius_km * math.sqrt(rng.random())
    theta = rng.random() * 2 * math.pi
    dlat = (r / 111.0) * math.cos(theta)
    dlng = (r / (111.0 * math.cos(math.radians(MM_LAT)))) * math.sin(theta)
    return Point(MM_LNG + dlng, MM_LAT + dlat, srid=4326)


def _require_throwaway_db():
    """Refuse to touch anything but a database whose name says it is disposable.

    `seed()` TRUNCATEs. This script takes its target from DATABASE_URL, which is one
    tab-completion away from `kupkop_dev` — and a load-check harness that can silently
    empty a working database is a much worse defect than any latency it might measure.
    """
    name = connection.settings_dict["NAME"]
    if "load" not in name:
        raise SystemExit(
            f"Refusing to seed '{name}': the load check truncates its tables, so its target "
            "database must have 'load' in the name (e.g. kupkop_load). Create one with:\n"
            "  psql -d postgres -c 'CREATE DATABASE kupkop_load;'\n"
            "  DATABASE_URL=postgres://localhost/kupkop_load python manage.py migrate")
    return name


def seed(scale: str) -> dict:
    cfg = SCALES[scale]
    rng = random.Random(20260904)      # fixed: a re-seed must produce the same shape
    now = timezone.now()
    cities = list(CITY_CENTROIDS)
    name = _require_throwaway_db()

    print(f"Seeding scale={scale} into {name} …")
    # Idempotent by wiping, not by get_or_create: a half-finished seed from an interrupted
    # run otherwise collides on account.email, and "seed it again" must be the fix rather
    # than a manual cleanup someone has to remember.
    with connection.cursor() as cur:
        cur.execute("TRUNCATE account, stray_report, adoption_listing, story_post, "
                    "account_capability RESTART IDENTITY CASCADE;")
    accounts = [
        Account(account_id=uuid.uuid4(), email=f"load{i}@kupkop.invalid",
                password_hash="!", display_name=f"Load User {i}",
                account_type="shelter" if i % 20 == 0 else "personal",
                status=AccountStatus.ACTIVE, email_verified_at=now)
        for i in range(cfg["accounts"])
    ]
    Account.objects.bulk_create(accounts, batch_size=2000)
    print(f"  accounts            {len(accounts):>7,}")

    # A listing is only publicly visible behind an approved capability (visibility.py's
    # public_poster_q). Seeding without these would leave GET /listings returning nothing
    # and "measuring" an empty result set — the classic way a load check reports a great
    # number for work it never did.
    shelters = [a for a in accounts if a.account_type == "shelter"]
    AccountCapability.objects.bulk_create(
        [AccountCapability(account=a, capability="rescuer", status="approved",
                           granted_at=now) for a in shelters], batch_size=1000)
    print(f"  approved rescuers   {len(shelters):>7,}")

    reports = []
    for i in range(cfg["reports"]):
        # ~40% lost/found so the L&F matching sweep has real work; the rest are strays.
        rtype = (ReportType.LOST if i % 5 == 0 else
                 ReportType.FOUND if i % 5 == 1 else ReportType.STRAY)
        reports.append(StrayReport(
            report_id=uuid.uuid4(), reporter_account=rng.choice(accounts),
            report_type=rtype, species=rng.choice(SPECIES),
            condition=rng.choice(CONDITIONS), geom=_scatter(rng),
            location_text="Seeded", status=StrayStatus.REPORTED,
            city=rng.choice(cities),
            created_at=now - timedelta(hours=rng.randint(0, 24 * 30)),
        ))
    StrayReport.objects.bulk_create(reports, batch_size=2000)
    print(f"  stray reports       {len(reports):>7,}")

    AdoptionListing.objects.bulk_create([
        AdoptionListing(listing_id=uuid.uuid4(), posted_by=rng.choice(shelters),
                        species=rng.choice(SPECIES), name=f"Pet {i}",
                        status="available", city=rng.choice(cities),
                        geom=_scatter(rng), created_at=now - timedelta(hours=rng.randint(0, 720)))
        for i in range(cfg["listings"])], batch_size=2000)
    print(f"  adoption listings   {cfg['listings']:>7,}")

    StoryPost.objects.bulk_create([
        StoryPost(story_id=uuid.uuid4(), author_account=rng.choice(accounts),
                  story_type="general", caption=f"Seeded story {i}. " * 8,
                  status="published",
                  created_at=now - timedelta(hours=rng.randint(0, 720)))
        for i in range(cfg["stories"])], batch_size=2000)
    print(f"  story posts         {cfg['stories']:>7,}")

    with connection.cursor() as cur:
        cur.execute("ANALYZE;")       # without this the planner is choosing on stale stats
    print("  ANALYZE done.")
    return cfg


# ── measuring ───────────────────────────────────────────────────────────────────────
def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


class Scenario:
    def __init__(self, name, kind, fn, n=60, warmup=5):
        self.name, self.kind, self.fn, self.n = name, kind, fn, n
        self.warmup = warmup


def timed(fn, n, warmup=5):
    """Time `fn` n times, after `warmup` discarded calls.

    ⚠️ warmup MUST be 0 for an idempotent batch job, and this was got wrong first.
    Warming up is right for a request — the first call through a Django process pays for
    lazy imports and an empty plan cache, a startup cost no user ever sees. It is exactly
    WRONG for `sweep_matches`, which is idempotent by design: the first run inserts every
    match and notifies, and every run after it only re-scores rows that already exist. So
    five warm-ups meant the "measured" sweep was a cheap re-run, reported as if it were
    the real cost — an error that understates precisely the number this scenario exists
    to expose. Warm-ups also are not free here: the sweep ran SIX times per measurement.
    """
    for _ in range(warmup):
        fn()
    samples, statuses = [], set()
    for _ in range(n):
        t0 = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - t0) * 1000)
        statuses.add(getattr(result, "status_code", 200))
    return samples, statuses


def build_scenarios(client, writers):
    manila = {"city": "Manila", "radius_km": "10"}

    def get(path, params=None):
        return lambda: client.get(path, params or {})

    # ⚠️ ONE ACCOUNT CANNOT DRIVE THE WRITE SCENARIO. `report_create` is throttled at
    # 20/day per account (US-SEC2), so a single actor's 21st POST is a 429 — served in
    # under a millisecond, which the first run of this script duly reported as a
    # gloriously fast write. A load check that measures its own rejections is worse than
    # no load check. Each request now comes from a different seeded account; the
    # `!! HTTP` marker below stays as the tripwire that caught it.
    writers = iter(writers)

    def post_report():
        actor = next(writers)
        auth = {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(actor)['access']}"}
        return client.post("/api/v1/reports", {
            "report_type": "stray", "species": "dog", "condition": "healthy",
            "lat": MM_LAT + random.uniform(-0.05, 0.05),
            "lng": MM_LNG + random.uniform(-0.05, 0.05),
            "location_text": "Load check",
            # US-O3's key is per-submission; a fixed one would make every call after the
            # first a dedup hit, which measures the idempotency lookup, not the write.
            "idempotency_key": uuid.uuid4().hex,
        }, content_type="application/json", **auth)

    def sweep():
        from sagip.matching import sweep_matches
        return sweep_matches()

    return [
        Scenario("GET /reports/map (10 km, Manila)", "geo", get("/api/v1/reports/map", manila)),
        Scenario("GET /reports/map (10 km + status filter)", "geo",
                 get("/api/v1/reports/map", {**manila, "status": "reported"})),
        Scenario("GET /listings (browse, page 1)", "read", get("/api/v1/listings")),
        Scenario("GET /listings?city=&species= (filtered)", "read",
                 get("/api/v1/listings", {"city": "Manila", "species": "dog"})),
        Scenario("GET /stories (feed)", "read", get("/api/v1/stories")),
        Scenario("POST /reports (write)", "write", post_report, n=40),
        # Not a request path and not in §13.1's table — measured because US-Q2 names it as
        # one of the two things that will move, and because a cron job that cannot finish
        # inside its own cadence is an outage that arrives quietly.
        # warmup=0: see timed(). This is a cold, first-run cost on a fresh match table,
        # which is the only figure that says anything about the night the sweep meets a
        # month of new reports.
        Scenario("sweep_matches() [cron, FIRST run]", "batch", sweep, n=1, warmup=0),
    ]


def explain_hot_queries():
    """US-Q2 asks for "the slowest queries" alongside the numbers.

    The obvious source, `pg_stat_statements`, is a shared_preload_libraries extension and
    is NOT present on a stock laptop Postgres — the first run of this script proved it by
    printing "unavailable" where the evidence should have been. EXPLAIN ANALYZE on the two
    spatial queries needs no extension and answers the question that actually matters:
    is the GiST index being used, or is this a sequential scan that only looks fast because
    the table is still small?
    """
    centroid = f"ST_GeogFromText('SRID=4326;POINT({MM_LNG} {MM_LAT})')"
    return [
        ("rescue map · ST_DWithin 10 km + distance sort", f"""
            SELECT report_id FROM stray_report
            WHERE ST_DWithin(geom, {centroid}, 10000)
            ORDER BY ST_Distance(geom, {centroid})"""),
        ("L&F matching · candidates for one report", f"""
            SELECT report_id FROM stray_report
            WHERE report_type = 'found' AND species = 'dog'
              AND ST_DWithin(geom, {centroid}, 5000)
              AND status <> 'resolved'"""),
    ]


def print_query_plans():
    print("\nQuery plans (EXPLAIN ANALYZE — pg_stat_statements is not installed on a "
          "stock laptop Postgres):")
    with connection.cursor() as cur:
        for label, sql in explain_hot_queries():
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, TIMING) " + sql)
            lines = [r[0] for r in cur.fetchall()]
            print(f"\n  {label}")
            for line in lines:
                # The interesting facts are the access method and the totals; the rest of
                # a plan is noise in a summary report.
                if any(k in line for k in ("Scan", "Sort", "Execution Time", "Planning Time")):
                    print(f"    {line.strip()[:150]}")


def measure(scale: str) -> int:
    cfg = SCALES[scale]
    counts = {
        "accounts": Account.objects.count(),
        "stray_report": StrayReport.objects.count(),
        "adoption_listing": AdoptionListing.objects.count(),
        "story_post": StoryPost.objects.count(),
    }
    if counts["stray_report"] < cfg["reports"] * 0.9:
        print(f"⚠️  Only {counts['stray_report']:,} reports present; expected ~{cfg['reports']:,}. "
              "Run --seed first — a p95 over an empty table measures nothing.", file=sys.stderr)
        return 2

    # 60 distinct writers: 45 measured POSTs + warm-ups, each under its own 20/day budget.
    writers = list(Account.objects.filter(status=AccountStatus.ACTIVE)[:60])
    client = Client()
    # The sweep emits one analytics line per suggested match — thousands of them, which
    # buries the results table it is printed above. The events themselves are US-E2 working
    # correctly; they just are not this script's output.
    logging.getLogger("kupkop.analytics").setLevel(logging.WARNING)

    print(f"\nRow counts (scale={scale}): " +
          " · ".join(f"{k} {v:,}" for k, v in counts.items()))
    print(f"\n{'Scenario':<44} {'p50':>8} {'p95':>8} {'max':>8} {'target':>8}  ")
    print("-" * 88)

    breaches = []
    for sc in build_scenarios(client, writers):
        samples, statuses = timed(sc.fn, sc.n, sc.warmup)
        p50, p95, mx = percentile(samples, 50), percentile(samples, 95), max(samples)
        target = TARGETS_MS[sc.kind]
        bad = statuses - {200, 201, 202}
        verdict = "—" if target is None else ("PASS" if p95 < target else "**BREACH**")
        if bad:
            verdict = f"!! HTTP {sorted(bad)}"     # a fast error is not a fast endpoint
        if target is not None and p95 >= target:
            breaches.append((sc.name, p95, target))
        tgt = "—" if target is None else f"<{target}"
        print(f"{sc.name:<44} {p50:>7.1f}ms {p95:>7.1f}ms {mx:>7.1f}ms {tgt:>8}  {verdict}")

    print_query_plans()

    print("\n⚠️  Local laptop, sequential requests, no network. Real p95 under concurrency "
          "will be higher — see this file's header.")
    if breaches:
        print(f"\n{len(breaches)} scenario(s) BREACH §13.1:")
        for name, p95, target in breaches:
            print(f"  {name}: p95 {p95:.1f}ms ≥ {target}ms")
        return 1
    print("\nNo §13.1 target breached at this scale.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--scale", choices=sorted(SCALES), default="year1")
    args = ap.parse_args(argv)
    if not (args.seed or args.measure):
        ap.error("pass --seed and/or --measure")
    if args.seed:
        seed(args.scale)
    return measure(args.scale) if args.measure else 0


if __name__ == "__main__":
    raise SystemExit(main())
