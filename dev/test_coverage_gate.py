"""US-Q1 · the gate's own logic, tested.

A coverage gate is a piece of CI that everyone trusts and nobody reads. Its failure mode is
not "wrong percentage" — it is passing when it should not, which is invisible by definition.
These tests exist for that failure mode specifically.
"""
import json

import pytest
from coverage_gate import THRESHOLDS, evaluate, render_summary


def report(**pkgs):
    """A coverage.py JSON report carrying just the fields the gate reads."""
    files = {}
    for pkg, (covered, total) in pkgs.items():
        files[f"{pkg}/views.py"] = {"summary": {
            "covered_lines": covered, "num_statements": total,
            "covered_branches": 0, "num_branches": 0,
        }}
    return {"files": files}


def test_a_package_below_its_threshold_fails():
    rows, failures = evaluate(report(sagip=(50, 100)), {"sagip": 96.0})
    assert failures
    assert rows[0].pct == pytest.approx(50.0)


def test_a_package_at_its_threshold_passes():
    # Exactly at the line is a pass. A gate set to today's number that fails on today's
    # number is a gate that gets deleted on its first run.
    _, failures = evaluate(report(sagip=(96, 100)), {"sagip": 96.0})
    assert failures == []


def test_a_package_with_no_measured_files_is_a_failure_not_a_pass():
    # THE reason this file exists. Rename a package, drop it from [tool.coverage.run]
    # source, or break the path prefix, and its files vanish from the report. Treating an
    # absent package as "nothing failed" turns the gate green at the exact moment it stopped
    # measuring the thing it was protecting.
    rows, failures = evaluate(report(sagip=(99, 100)), {"sagip": 96.0, "volunteer": 93.0})
    assert any(f.package == "volunteer" for f in failures)
    assert any("no measured files" in f.reason for f in failures)


def test_branches_count_toward_the_percentage():
    # §15.4 wants a gate with teeth; line-only coverage calls an untested `else` branch
    # covered. pyproject sets branch = true, so the gate must read those columns or it
    # silently reports a laxer number than the one CI measured.
    r = {"files": {"sagip/geo.py": {"summary": {
        "covered_lines": 10, "num_statements": 10,
        "covered_branches": 0, "num_branches": 10,
    }}}}
    rows, _ = evaluate(r, {"sagip": 0.0})
    assert rows[0].pct == pytest.approx(50.0)


def test_a_package_outside_the_thresholds_is_reported_but_never_gates():
    # `common` and friends are measured for the trend. Gating a package nobody agreed to
    # gate is how a gate acquires a reputation for blocking merges over nothing.
    rows, failures = evaluate(report(sagip=(99, 100), moderation=(1, 100)), {"sagip": 96.0})
    assert failures == []
    assert {r.package for r in rows} == {"sagip", "moderation"}
    assert [r.gated for r in rows if r.package == "moderation"] == [False]


def test_the_summary_names_the_number_the_threshold_and_the_direction():
    # §15.4 asks for a trend that is *visible*. A number with no threshold beside it tells
    # a reader nothing about whether it is good.
    rows, _ = evaluate(report(sagip=(97, 100)), {"sagip": 96.0})
    out = render_summary(rows)
    assert "sagip" in out and "97" in out and "96" in out


def test_every_welfare_critical_package_of_d_s7_6_is_actually_gated():
    # D-S7-6 names six. A threshold table that quietly loses one is the gap this asserts
    # against — the table is the decision, and the decision is reviewable only if it is
    # checked against something.
    assert set(THRESHOLDS) >= {
        "accounts", "community", "listings", "sagip", "verifications", "volunteer",
    }


def test_it_reads_a_real_report_from_disk(tmp_path):
    p = tmp_path / "cov.json"
    p.write_text(json.dumps(report(sagip=(99, 100))))
    from coverage_gate import load

    assert load(str(p))["files"]
