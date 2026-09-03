"""US-Y1 · server-side, account-free analytics (D-S6-6).

`emit()` writes one structured JSON log line per server-authoritative §17.2 outcome —
resolutions, completions, decisions — that a log pipeline or the admin dashboard can aggregate.
Deliberately narrow:

  - NO account identifiers or PII in the payload. This MVP is aggregate-only because
    `account_settings` has no analytics-consent column yet and §17 says honor consent per §12.6.
  - Client-side events (session_start, tab_viewed, push_opened, …) and any per-user analytics
    are DELIBERATELY ABSENT until Sprint 7 lands the consent column + settings UI. Do not add
    them here before that — the whole point of keeping this server-side is that server outcomes
    don't need per-user consent, while client/behavioural events do.

Fire-and-forget: `emit()` never raises. A logging failure must never break the request that
produced the event (the `notifications/push.py::send_push` posture).
"""
import json
import logging

from django.utils import timezone

logger = logging.getLogger("kupkop.analytics")


def emit(event, **props):
    """Record a server-authoritative analytics event. `props` are coarse, non-identifying
    dimensions (e.g. type, species, outcome, score_bucket) — never an account id, email, or
    other PII. Never raises."""
    try:
        payload = {"event": event, "ts": timezone.now().isoformat()}
        payload.update(props)
        logger.info(json.dumps(payload, default=str))
    except Exception:
        logger.exception("analytics emit failed")   # never propagate to the caller
