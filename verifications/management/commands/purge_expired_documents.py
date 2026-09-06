"""US-SEC4 · run the identity-document retention purge once.

    python manage.py purge_expired_documents

Meant to run beside `sagip`'s `run_sweeps` (same cron, separate command — the two sweep
domains don't share a scheduler dependency). No scheduler is embedded here, matching
US-F0's decision (cron/command over Celery-beat for MVP).
"""
from common.management_base import SingletonCommand
from verifications.purge import purge_expired_documents


class Command(SingletonCommand):
    help = "Delete and null file_url for verification documents past the retention window."

    def run(self, *args, **options):
        purged = purge_expired_documents()
        self.stdout.write(self.style.SUCCESS(f"purged {len(purged)} document(s)"))
