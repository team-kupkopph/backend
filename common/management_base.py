"""US-Q2 follow-up · the base class for cron-invoked management commands.

Every scheduled command in this codebase is periodic and re-entrant-unsafe in the same way,
so the guard belongs in one place rather than copy-pasted into each `handle()`. A fourth
scheduled command added later gets it by inheriting, which is the point: the failure mode
of a per-command guard is the command that quietly does not have one.
"""
import logging

from django.core.management.base import BaseCommand

from common.locks import LockBusy, advisory_lock

logger = logging.getLogger("kupkop.locks")


class SingletonCommand(BaseCommand):
    """A command that refuses to run twice at once, cluster-wide.

    Subclasses implement `run()` instead of `handle()`. `lock_name` defaults to the
    command's module name, so each job contends only with itself — the RA 10173 purge must
    never serialize behind an unrelated hourly sweep (US-N2 gave it its own cron entry
    precisely so sweep cadence cannot govern how fast personal data is destroyed).
    """

    lock_name: str | None = None

    def _lock_name(self) -> str:
        return self.lock_name or self.__module__.rsplit(".", 1)[-1]

    def handle(self, *args, **options):
        name = self._lock_name()
        try:
            with advisory_lock(name):
                return self.run(*args, **options)
        except LockBusy:
            # Exit ZERO. A skipped tick is normal operation for a periodic job, and cron
            # mails (and alerting pages) on non-zero — reporting it as a failure trains
            # everyone to mute the job, and the mute outlives the condition. It is logged
            # at WARNING so US-E2's structured stream still carries it.
            logger.warning({"event": "scheduled_run_skipped", "command": name,
                            "reason": "previous run still in progress"})
            self.stdout.write(self.style.WARNING(
                f"{name}: skipped — a previous run is already running."))

    def run(self, *args, **options):
        raise NotImplementedError
