"""Review step 24 (MQ9) · scrub the free text of old dismissed reports once.

    python manage.py scrub_flag_reasons

Its own command, like the other retention jobs, because it destroys data and should be schedulable and auditable apart from the
hourly sweeps. See moderation/retention.py for the rule.
"""
from common.management_base import SingletonCommand
from moderation.retention import scrub_old_reasons


class Command(SingletonCommand):
    help = "Blank the free text of dismissed reports older than the retention window (RA 10173, MQ9)."

    def run(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(f"scrubbed {scrub_old_reasons()} report(s)"))
