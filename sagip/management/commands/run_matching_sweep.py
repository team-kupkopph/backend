"""US-Q2 follow-up · the §11.4 NIGHTLY lost-and-found matching safety net.

    python manage.py run_matching_sweep

Its own command, and its own cron line, for the same reason `purge_deleted_accounts` has
one (US-N2): a job whose correct cadence differs from the hourly sweeps must not inherit
theirs by being folded into the same entry. It had been inside `run_sweeps`, so §11.4's
*nightly* safety net was running 24x a day.

⚠️ THIS IS THE SAFETY NET, NOT THE MATCHER. §11.4 has two triggers: a synchronous candidate
scan when a lost/found report is filed (`sagip/views.py`, and it is what an actual reunion
depends on), and this nightly re-scan that catches near-miss pairs — a report that scored
just under threshold yesterday, before the other half existed or before somebody added a
breed. Nobody waits on this run; that is exactly why it can be nightly.

⚠️ AND IT IS NOT CHEAP. US-Q2 measured it at 18.5 s over 5,000 reports and 687.6 s — 11.5
minutes — over 50,000: 37x the time for 10x the data, roughly O(n^1.57), because it runs one
spatial query per open report over a candidate set that also grows with density. Hourly, that
was 11.5 minutes of database load every hour, competing with the requests §13.1 budgets.
"""
from common.management_base import SingletonCommand
from sagip.matching import sweep_matches


class Command(SingletonCommand):
    help = "§11.4's nightly lost<->found matching safety net (re-scores still-open reports)."

    def run(self, *args, **options):
        matched = sweep_matches()
        self.stdout.write(self.style.SUCCESS(f"matched {len(matched)} pair(s)"))
