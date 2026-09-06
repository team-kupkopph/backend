#!/usr/bin/env python3
"""US-Q1 · per-package coverage gate + trend summary.

Tech Spec §15.4: "Coverage tracked as a trend, with a hard gate on the welfare-critical
packages rather than a blanket global %." coverage.py can enforce exactly one number for a
whole run (`--cov-fail-under`), which is the blanket gate §15.4 explicitly does not want:
it lets `sagip` rot while `moderation` carries the average. This script reads the JSON
report and applies one threshold per package instead.

    python dev/coverage_gate.py coverage.json [--summary $GITHUB_STEP_SUMMARY]

Exit 1 on any breach, naming the package, its number and its threshold.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

# THE RATCHET. Each threshold was set from the measured 2026-09-04 baseline, floored to the
# whole point at or just below it — deliberately, per US-Q1: a gate set where the code
# aspires to be is a gate that fails on day one and gets bypassed in week two, so it never
# protects anything. Set here, it fails only on a real regression.
#
#   measured 2026-09-04 (758 tests, statements+branches):
#     accounts 95.87 · community 93.31 · listings 96.61 · sagip 97.07
#     verifications 96.55 · volunteer 93.61 · common 90.25
#
# ⚠️ HEADROOM IS COUNTED IN LINES, NOT IN POINTS. `common` is ~320 measured units, so one
# uncovered line moves it a third of a point: the round number below it (90) leaves under a
# line of slack and would fire on the next ordinary PR that adds a small helper — which is
# the "gets bypassed in week 2" failure this whole story exists to avoid. `common` and
# `community` are therefore set a point lower than the round-down, for ~4-6 lines of slack
# each, the same absolute cushion the larger packages get for free.
#
# To ratchet: raise a number when the package has held above it for a sprint. Never lower
# one to make a build green — lowering it is a decision that belongs in a review, which is
# why the numbers live in a committed file and not in a CI flag.
THRESHOLDS: dict[str, float] = {
    # D-S7-6's six welfare-critical packages (§15.3's paths live in these).
    "accounts": 95.0,        # onboarding, OTP, consent, export/delete
    "community": 92.0,       # stories, needs, pledges, badges
    "listings": 96.0,        # adoption post → inquire → approve/decline
    "sagip": 96.0,           # the rescue loop, matching, §12.5 coordinates
    "verifications": 96.0,   # the trust gate everything else keys off
    "volunteer": 93.0,       # shifts, capacity, check-in/out
    # Not in D-S7-6, gated anyway: every one of the six routes its errors, throttles, OTP
    # and media through here, so a hole in `common` is a hole in all six.
    "common": 89.0,
}


@dataclass
class Row:
    package: str
    covered: int
    total: int
    pct: float
    threshold: float | None
    gated: bool


@dataclass
class Failure:
    package: str
    reason: str


def load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def evaluate(report: dict, thresholds: dict[str, float]) -> tuple[list[Row], list[Failure]]:
    """Aggregate the report by top-level package and compare against `thresholds`."""
    agg: dict[str, list[int]] = {}
    for path, entry in report.get("files", {}).items():
        pkg = path.split("/")[0]
        s = entry["summary"]
        a = agg.setdefault(pkg, [0, 0])
        # Statements AND branches. pyproject sets branch = true; counting only lines would
        # report a laxer number than the one the run actually measured.
        a[0] += s["covered_lines"] + s.get("covered_branches", 0)
        a[1] += s["num_statements"] + s.get("num_branches", 0)

    rows, failures = [], []
    for pkg in sorted(set(agg) | set(thresholds)):
        threshold = thresholds.get(pkg)
        if pkg not in agg:
            # Not "nothing to check" — the gate stopped measuring a package it is supposed
            # to protect (renamed app, dropped from [tool.coverage.run] source, moved path).
            # Silence here is the one way this script can fail open.
            failures.append(Failure(pkg, f"no measured files in the report (threshold {threshold:.0f}%)"))
            rows.append(Row(pkg, 0, 0, 0.0, threshold, True))
            continue
        covered, total = agg[pkg]
        pct = 100.0 * covered / total if total else 0.0
        gated = threshold is not None
        rows.append(Row(pkg, covered, total, pct, threshold, gated))
        if gated and pct < threshold:
            failures.append(Failure(pkg, f"{pct:.2f}% is below the {threshold:.0f}% threshold"))
    return rows, failures


def render_summary(rows: list[Row]) -> str:
    """A markdown table for the GitHub job summary — §15.4's 'tracked as a trend' half.

    A trend nobody can see is a number in a log. This renders on the run's own page.
    """
    out = ["### Coverage by package", "",
           "| Package | Coverage | Threshold | |", "|---|---:|---:|---|"]
    for r in sorted(rows, key=lambda r: (not r.gated, r.package)):
        if r.threshold is None:
            mark, thr = "·", "—"
        elif r.total == 0:
            mark, thr = "❌ not measured", f"{r.threshold:.0f}%"
        else:
            mark = "✅" if r.pct >= r.threshold else "❌"
            thr = f"{r.threshold:.0f}%"
        out.append(f"| `{r.package}` | {r.pct:.2f}% | {thr} | {mark} |")
    out += ["", "Thresholds are a ratchet set from the measured baseline "
                "(`dev/coverage_gate.py`); raising one is a PR, lowering one is a decision."]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("report", help="path to coverage.py's JSON report")
    ap.add_argument("--summary", help="also append the markdown table to this file")
    args = ap.parse_args(argv)

    rows, failures = evaluate(load(args.report), THRESHOLDS)
    table = render_summary(rows)
    print(table)
    if args.summary:
        with open(args.summary, "a") as fh:
            fh.write(table + "\n")

    if failures:
        print("\nCoverage gate FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f.package}: {f.reason}", file=sys.stderr)
        return 1
    print("\nCoverage gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
