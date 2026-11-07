"""Review step 23 (MQ8) · tell staff when a report has waited too long.

Apple 1.2 wants "timely responses to concerns" and neither store names a number. MQ8 recommended 24 hours, and 4 for an animal
in danger. A target is only as good as the thing that notices it being missed: without this, an overdue report is found by whoever
happens to open the queue.

One email per run that has something new to say, not one per flag and not one every hour: a flag is stamped `alerted_at` when staff
are told about it. The email carries counts and ages, never a reporter's words or a name (a mailbox is not a safe place for either),
and points to the console.

⚠️ WITH NO RECIPIENT CONFIGURED NOTHING IS STAMPED. Marking a flag "alerted" when nobody was told would hide it from the one
alert that exists; it is logged as a warning every run until someone is named (`MODERATION_ALERT_EMAILS`).
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from common.senders import get_sender
from moderation.models import FlagStatus, ModerationFlag, ReasonCode

logger = logging.getLogger("kupkop.moderation")


def overdue_flags(now=None):
    """Open flags past their response target and not yet alerted: a welfare concern at MODERATION_WELFARE_HOURS, anything else at
    MODERATION_RESPONSE_HOURS. `reviewed` is not waiting for a first look, so it does not count."""
    now = now or timezone.now()
    general = now - timedelta(hours=settings.MODERATION_RESPONSE_HOURS)
    welfare = now - timedelta(hours=settings.MODERATION_WELFARE_HOURS)
    waiting = ModerationFlag.objects.filter(status=FlagStatus.OPEN, alerted_at__isnull=True)
    return [f for f in waiting.order_by("created_at")
            if f.created_at <= (welfare if f.reason_code == ReasonCode.ANIMAL_WELFARE else general)]


def _hours(flag, now):
    return int((now - flag.created_at).total_seconds() // 3600)


def alert_overdue_flags(now=None):
    """Email staff about flags that have newly passed their target. Returns the flags it told them about (the sweep contract)."""
    now = now or timezone.now()
    late = overdue_flags(now)
    if not late:
        return []
    recipients = settings.MODERATION_ALERT_EMAILS
    if not recipients:
        logger.warning("%d report(s) are past their response target and MODERATION_ALERT_EMAILS is empty: nobody was told", len(late))
        return []
    welfare = [f for f in late if f.reason_code == ReasonCode.ANIMAL_WELFARE]
    lines = [f"{len(late)} report(s) are past their response target:"]
    if welfare:
        lines.append(f"  - {len(welfare)} about an animal that may be in danger (target {settings.MODERATION_WELFARE_HOURS}h), "
                     f"oldest {_hours(welfare[0], now)}h")
    rest = [f for f in late if f.reason_code != ReasonCode.ANIMAL_WELFARE]
    if rest:
        lines.append(f"  - {len(rest)} other (target {settings.MODERATION_RESPONSE_HOURS}h), oldest {_hours(rest[0], now)}h")
    lines.append("Open the Moderation queue in the console, oldest first.")
    sender, sent = get_sender(), 0
    for to in recipients:
        try:
            sender.send_notice(to=to, subject=f"Kupkop: {len(late)} report(s) past their response target", body="\n".join(lines))
            sent += 1
        except Exception:
            logger.exception("could not send the overdue-report alert to one recipient")
    if not sent:
        return []                    # nobody was told: stamp nothing, so the next run tries again
    ModerationFlag.objects.filter(pk__in=[f.pk for f in late]).update(alerted_at=now)
    return late
