"""US-F0 / US-N2 / US-V7 · invoke every scheduled sweep once.

    python manage.py run_sweeps

One command, one cron entry, however many domains own sweeps. Sweeps were multiplying
across apps (sagip's three, now volunteer's reminders) and a per-app command would mean a
per-app cron line to forget. No scheduler is embedded here — US-F0's decision (cron over
Celery-beat) still stands, and D-S5-6 reconfirmed it for push.

⚠️ `purge_expired_documents` (US-SEC4) stays its own command deliberately: retention
deletion is destructive and should be schedulable — and auditable — independently of the
routine hourly sweeps.
"""
from django.core.management.base import BaseCommand

from community.sweeps import award_badges
from sagip.matching import sweep_matches
from sagip.sweeps import escalate_reports, expire_offers, expire_stalled_claims
from volunteer.sweeps import remind_shifts

# (label, callable) — each returns a list of the rows it touched.
SWEEPS = [
    ("escalated", escalate_reports),
    ("expired", expire_stalled_claims),
    ("reminded", remind_shifts),
    ("badged", award_badges),
    ("matched", sweep_matches),
]


class Command(BaseCommand):
    help = "Run every scheduled sweep: escalation, stalled claims, offer expiry, shift reminders."

    def handle(self, *args, **options):
        parts = [f"{label} {len(fn())}" for label, fn in SWEEPS]
        # expire_offers returns a count, not a list — kept separate rather than forcing a
        # uniform return type on a sweep that has no rows worth handing back.
        parts.append(f"expired {expire_offers()} offer(s)")
        self.stdout.write(self.style.SUCCESS(", ".join(parts)))
