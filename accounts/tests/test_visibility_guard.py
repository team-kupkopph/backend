"""AS5 / X5 · nobody writes their own spelling of "publicly visible" (plan Task 5).

The bug this exists for: listings and stories excluded only DELETED accounts while needs, shifts, the QR and
Sagip alerts filtered on ACTIVE, so a SUSPENDED poster stayed fully public. Several hand-written spellings of
one rule drift apart. `accounts/visibility.py` is the only place that decides; a source scan fails if a
relation-prefixed account-status filter (`posted_by__status=AccountStatus.X`, `shelter_account__status="active"`,
`.exclude(...__status=...)`) appears anywhere else in the application code.

⚠️ Every source-scan guard in this project has under-reported at least once, so this one first proves it is
looking at the right tree (the helper is there, and so are the modules that used to carry the old spellings)
and that its pattern catches the old spellings, before it is allowed to report "nothing found".
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".venv", ".worktrees", "migrations", "tests", "node_modules", "dev", "__pycache__"}

# `<relation>__status` compared to an ACCOUNT status: the enum, or the literal strings of its values.
ACCOUNT_STATUS_LOOKUP = re.compile(
    r"\b\w+__status\s*(?:__in\s*)?=\s*(?:AccountStatus\.\w+|[\[(]?\s*[\"'](?:active|suspended|deleted)[\"'])")


def _application_files():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if set(rel.parts) & SKIP_DIRS or path.name.startswith("test_"):
            continue
        yield rel


def _hits():
    found = []
    for rel in _application_files():
        for number, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
            if ACCOUNT_STATUS_LOOKUP.search(line):
                found.append(f"{rel}:{number}: {line.strip()}")
    return found


def test_the_scan_looks_at_the_right_tree():
    files = {str(rel) for rel in _application_files()}

    assert "accounts/visibility.py" in files                      # the helper's own home
    for must in ("listings/views.py", "listings/visibility.py", "community/views.py", "community/needs.py",
                 "volunteer/visibility.py", "shelter/views.py", "sagip/alerts.py"):
        assert must in files, f"the scan did not reach {must}"
    assert len(files) > 100


def test_the_pattern_catches_the_old_spellings_and_ignores_other_statuses():
    old = [
        ".exclude(posted_by__status=AccountStatus.DELETED)",
        '.filter(shelter_account__status="active")',
        "account__status=AccountStatus.ACTIVE).exists()",
        ".exclude(author_account__status=AccountStatus.DELETED)",
        "posted_by__status__in=['suspended', 'deleted']",
    ]
    unrelated = [
        "filter=Q(inquiries__status=InquiryStatus.ACTIVE)",
        "pledges__status=PledgeStatus.PLEDGED",
        "report__status=StrayStatus.RESOLVED",
        'status="active"',                                          # a plain column, not a relation lookup
        "posted_by__capabilities__status=\"approved\"",
    ]

    assert all(ACCOUNT_STATUS_LOOKUP.search(line) for line in old)
    assert not any(ACCOUNT_STATUS_LOOKUP.search(line) for line in unrelated)


def test_no_account_status_lookup_outside_the_helper():
    assert _hits() == [], (
        "Decide who is publicly visible in accounts/visibility.py (`public_account_q(prefix)`), "
        "not with a hand-written account-status filter:\n" + "\n".join(_hits()))
