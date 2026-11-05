"""IB23 · the nightly badge reconciliation.

    python manage.py run_badge_sweep

Its own command and cron line, for the reason `run_matching_sweep` has one: a job whose right
cadence is not hourly must not inherit it. The event hooks award a badge the moment it is earned
(`award_after_commit`), so nobody waits on this: it catches a hook that missed, or a badge
newly seeded.

⚠️ EXIT CODE. A partial failure exits zero, and says so in the summary: one bad account is
logged and retried tomorrow, and a cron mail for it would be muted within a week. Only a run
where something failed and nothing succeeded is a failed run, because that is a broken sweep,
not a bad row.
"""
from django.core.management.base import CommandError

from common.management_base import SingletonCommand
from community.sweeps import award_badges


class Command(SingletonCommand):
    help = "Nightly badge reconciliation: award what an account qualifies for and does not hold."

    def run(self, *args, **options):
        touched, failed = award_badges()
        summary = f"badged {len(touched)}, failed {len(failed)}"
        if failed and not touched:
            raise CommandError(summary)
        self.stdout.write((self.style.WARNING if failed else self.style.SUCCESS)(summary))
