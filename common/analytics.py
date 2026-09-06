"""US-Y1 · server-side, account-free analytics (D-S6-6).

`emit()` writes one structured JSON log line per server-authoritative §17.2 outcome —
resolutions, completions, decisions — that a log pipeline or the admin dashboard can aggregate.
Deliberately narrow:

  - NO account identifiers or PII in the payload. That is what lets these events flow with
    no consent at all: §17's tooling note says server-authoritative outcomes need none
    precisely because they carry nothing personal.
  - Client-side events (session_start, tab_viewed, push_opened, …) are STILL absent, but the
    reason has changed. Sprint 7 landed `account_settings.analytics_consent` (US-N3/N5), so
    the consent gate now exists — what does not exist is a client analytics SDK, which
    D-S7-3 deliberately left to Phase 2. Anything added here must stay server-side and
    account-free; a per-user event belongs behind that consent flag, not in this module.

Routing: since US-E2 these lines are formatted by `common/observability.py::JsonFormatter`
and land on stdout as one JSON object per line, carrying `logger: "kupkop.analytics"` so a
pipeline can select them by field.

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
