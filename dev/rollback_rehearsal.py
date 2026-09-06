#!/usr/bin/env python3
"""US-E3 · rehearse the rollback. §16.6: "rollback path rehearsed once".

    python dev/rollback_rehearsal.py --previous 27254b8

⚠️ WHICH ROLLBACK THIS REHEARSES, because there are two and only one of them is the plan.

§16.3 describes production rollback as "auto-rollback to the previous task definition" —
the app image goes back, **the database stays migrated forward**. Migrations run as their
own pipeline step *before* the deploy, so by the time a rollback happens the new schema is
already there and is not coming back out. That makes the real question:

    Does the PREVIOUS release's code still work against the NEW schema?

If the answer is no, there is no rollback path at all — only a forward fix under incident
pressure, which is the worst place to write code. §16.3's expand/contract rule exists to
keep the answer yes, and this script is what checks that the rule was actually followed.

PATH B, also checked: can the new migrations be reversed at all? That is the escape hatch
for when path A fails, and `migrate <app> <previous>` either works or it does not — better
to learn which on a Tuesday than during an outage.

Both paths run against a throwaway database. The script refuses any database whose name
does not contain "rollback".
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(BACKEND, ".venv", "bin", "python")

# The migration state of the previous release (Sprint 6 head, backend 27254b8). Sprint 7
# added eight migrations across six apps; these are the targets to roll back TO.
PREVIOUS_RELEASE = {
    "accounts": "0006", "listings": "0003", "moderation": "0002",
    "verifications": "0006", "community": "0004", "sagip": "0005",
}

ENV_BASE = {
    "DJANGO_SECRET_KEY": "rollback-rehearsal-throwaway-key",
    "DJANGO_ALLOWED_HOSTS": "testserver,localhost,127.0.0.1",
}


def run(args, db, cwd=BACKEND, check=True, quiet=False):
    env = {**os.environ, **ENV_BASE, "DATABASE_URL": f"postgres://localhost/{db}"}
    proc = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if not quiet:
        tail = (proc.stdout or proc.stderr).strip().splitlines()[-3:]
        for line in tail:
            print(f"      {line[:150]}")
    if check and proc.returncode != 0:
        print(f"\n  COMMAND FAILED: {' '.join(args)}\n{proc.stdout}\n{proc.stderr}",
              file=sys.stderr)
    return proc


def psql(db, sql):
    return subprocess.run(["psql", "-d", db, "-tAc", sql],
                          capture_output=True, text=True).stdout.strip()


def fresh_db(db):
    if "rollback" not in db:
        raise SystemExit(f"Refusing to use '{db}': this script DROPs its database. "
                         "Name it something containing 'rollback'.")
    subprocess.run(["psql", "-d", "postgres", "-c", f"DROP DATABASE IF EXISTS {db}"],
                   capture_output=True, text=True)
    subprocess.run(["psql", "-d", "postgres", "-c", f"CREATE DATABASE {db}"],
                   capture_output=True, text=True)


def seed_row(db):
    """One account + one report, written by the NEW code on the NEW schema.

    The rollback must not lose it. Data written between the deploy and the rollback is
    exactly the data a naive rollback destroys, so it is the only data worth checking.
    """
    script = (
        "import django, os, uuid;"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings');"
        "django.setup();"
        "from accounts.models import Account;"
        "from django.contrib.gis.geos import Point;"
        "from sagip.models import StrayReport;"
        "a=Account.objects.create(account_id=uuid.uuid4(), email='rehearsal@kupkop.invalid',"
        " password_hash='!', display_name='Rehearsal', account_type='personal');"
        "StrayReport.objects.create(report_id=uuid.uuid4(), reporter_account=a,"
        " report_type='stray', species='dog', condition='healthy',"
        " geom=Point(121.0, 14.6, srid=4326), location_text='Rehearsal',"
        # The Sprint 7 column specifically: a row that only the new schema can hold.
        " idempotency_key='rehearsal-key');"
        "print('seeded')")
    return run([PY, "-c", script], db, quiet=True)


def counts(db):
    return (psql(db, "select count(*) from account where email='rehearsal@kupkop.invalid'"),
            psql(db, "select count(*) from stray_report where location_text='Rehearsal'"))


def path_a(db, previous_ref, tmpdir) -> bool:
    """Previous release's code against the new schema — the rollback §16.3 actually plans."""
    print("\n── PATH A · previous release's code vs. the NEW schema (§16.3's real rollback)")
    fresh_db(db)
    print("  [1] migrate to the CURRENT release's schema")
    run([PY, "manage.py", "migrate", "--no-input"], db)
    print("  [2] write a row the way the new code does")
    seed_row(db)
    print(f"      account/report rows: {counts(db)}")

    print(f"  [3] check out the previous release ({previous_ref}) into a worktree")
    prev = os.path.join(tmpdir, "previous")
    add = subprocess.run(["git", "worktree", "add", "--detach", prev, previous_ref],
                         cwd=BACKEND, capture_output=True, text=True)
    if add.returncode != 0:
        print(f"      could not create worktree: {add.stderr.strip()[:200]}")
        return False

    print("  [4] the OLD code's own checks, against the NEW database")
    # `check` loads every model, URL, serializer and app config against the live settings.
    # It does not query, so it proves the code IMPORTS; the migrate --check below is what
    # proves the two schemas are actually compatible.
    chk = run([PY, "manage.py", "check"], db, cwd=prev, check=False)

    print("  [5] how far ahead of the old code is this schema?")
    # NOT `showmigrations` from the old worktree: the old code cannot see migrations that
    # do not exist in its own tree, so it would cheerfully report "nothing unapplied" no
    # matter how far ahead the database is. The honest measure compares what the DATABASE
    # says has been applied against what the old code KNOWS about. A non-zero gap is the
    # normal, intended state under expand/contract — the point is to print the number, so
    # that "the old code ran fine" is understood as "ran fine against a schema N migrations
    # ahead of it", not as "the schemas happened to be identical".
    show = run([PY, "manage.py", "showmigrations", "--plan"], db, cwd=prev,
               check=False, quiet=True)
    known = len([ln for ln in show.stdout.splitlines() if ln.strip().startswith("[")])
    applied = int(psql(db, "select count(*) from django_migrations") or 0)
    ahead = max(0, applied - known)
    print(f"      database has {applied} applied; the old code knows {known} → "
          f"{ahead} ahead")

    print("  [6] can the OLD code still read and write the table the new schema changed?")
    probe = (
        "import django, os, uuid;"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings');"
        "django.setup();"
        "from accounts.models import Account;"
        "from django.contrib.gis.geos import Point;"
        "from sagip.models import StrayReport;"
        "assert Account.objects.filter(email='rehearsal@kupkop.invalid').exists();"
        "a=Account.objects.get(email='rehearsal@kupkop.invalid');"
        "assert StrayReport.objects.filter(location_text='Rehearsal').count()==1;"
        "StrayReport.objects.create(report_id=uuid.uuid4(), reporter_account=a,"
        " report_type='stray', species='cat', condition='healthy',"
        " geom=Point(121.1, 14.7, srid=4326), location_text='OldCodeWrite');"
        "print('OLD CODE read and wrote the new schema OK')")
    rw = run([PY, "-c", probe], db, cwd=prev, check=False)

    subprocess.run(["git", "worktree", "remove", "--force", prev],
                   cwd=BACKEND, capture_output=True, text=True)
    ok = chk.returncode == 0 and rw.returncode == 0
    print(f"  → PATH A: {'ROLLBACK IS SAFE' if ok else 'ROLLBACK WOULD BREAK'}"
          f" — old code ran against a schema {ahead} migration(s) ahead of it")
    return ok


def path_b(db) -> bool:
    """Can the new migrations be reversed? The escape hatch, if path A ever fails."""
    print("\n── PATH B · can the Sprint 7 migrations be un-applied at all?")
    fresh_db(db)
    run([PY, "manage.py", "migrate", "--no-input"], db, quiet=True)
    seed_row(db)
    before = counts(db)
    print(f"  [1] at head, rows: {before}")

    print("  [2] roll the schema back to the previous release, app by app")
    ok = True
    for app, target in PREVIOUS_RELEASE.items():
        proc = run([PY, "manage.py", "migrate", app, target, "--no-input"], db,
                   check=False, quiet=True)
        state = "ok" if proc.returncode == 0 else "IRREVERSIBLE"
        if proc.returncode != 0:
            ok = False
            print(f"      {app} → {target}: {state} — "
                  f"{(proc.stderr or proc.stdout).strip().splitlines()[-1][:110]}")
        else:
            print(f"      {app} → {target}: {state}")

    after = counts(db)
    print(f"  [3] after rollback, rows: {after}  "
          f"({'PRESERVED' if after == before else 'DATA LOST'})")

    print("  [4] roll forward again (a rollback you cannot undo is a one-way door)")
    fwd = run([PY, "manage.py", "migrate", "--no-input"], db, check=False, quiet=True)
    final = counts(db)
    print(f"  [5] back at head, rows: {final}")
    ok = ok and fwd.returncode == 0 and after == before
    print(f"  → PATH B: {'REVERSIBLE' if ok else 'NOT FULLY REVERSIBLE'}")
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--previous", default="27254b8",
                    help="git ref of the previous release (default: Sprint 6 head)")
    ap.add_argument("--db", default="kupkop_rollback")
    args = ap.parse_args(argv)

    print("US-E3 · ROLLBACK REHEARSAL")
    print(f"  current: {subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=BACKEND, capture_output=True, text=True).stdout.strip()}"
          f"   previous: {args.previous}   database: {args.db}")
    with tempfile.TemporaryDirectory() as tmp:
        a = path_a(args.db, args.previous, tmp)
    b = path_b(args.db)

    print("\n" + "=" * 74)
    print(f"PATH A (revert the app, keep the schema — §16.3's plan): "
          f"{'PASS' if a else 'FAIL'}")
    print(f"PATH B (un-apply the migrations — the escape hatch):     "
          f"{'PASS' if b else 'FAIL'}")
    print("=" * 74)
    print("⚠️  This rehearses the DATA-LAYER rollback only. The ECS task-definition "
          "rollback\n    §16.3 describes cannot be rehearsed: there is no deployed "
          "environment (US-D3).")
    return 0 if a else 1        # path B failing is a known-risk, path A failing is a stop


if __name__ == "__main__":
    raise SystemExit(main())
