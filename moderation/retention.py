"""Review step 24 (MQ9) · how long a report's free text is kept.

A report's `reason` is a stranger's words about another person, written in a moment of upset. Keeping it forever is a liability
for the reporter and the reported alike (RA 10173: keep personal data no longer than the purpose needs). MQ9's rule:

  - a DISMISSED report's text is scrubbed MODERATION_REASON_RETENTION_DAYS (12 months) after it was decided;
  - an ACTIONED report's text is kept: it is part of the welfare record that justified the takedown;
  - an open or reviewed report is still being worked, so it is never touched.

Only the text goes. The row stays (who, what, when, category, outcome), so counts and repeat-offender views are unaffected, and the
category is kept because it is not free text. Idempotent: a scrubbed row has nothing left to scrub.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from moderation.models import FlagStatus, ModerationFlag


def scrub_old_reasons(now=None):
    """Blank the `reason` of dismissed flags decided before the retention window. Returns how many rows it blanked."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=settings.MODERATION_REASON_RETENTION_DAYS)
    return (ModerationFlag.objects.filter(status=FlagStatus.DISMISSED, reviewed_at__lte=cutoff)
            .exclude(reason="").update(reason=""))
