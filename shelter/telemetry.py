"""SH16 (shelter home review) · the shelter's account-free analytics events.

Through `common.analytics.emit`, so the same rules hold: coarse dimensions only, never an account id,
a name or a field value. `age_bucket` is what makes time to first response measurable without one
(SQ2, 2026-11-04: no per-shelter response-time metric yet).
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from common.analytics import emit

_AGE_BUCKETS = (
    (timedelta(hours=1), "lt_1h"),
    (timedelta(hours=24), "1h_24h"),
    (timedelta(days=3), "1d_3d"),
    (timedelta(days=7), "3d_7d"),
)


def age_bucket(created_at, now=None):
    """How long a request waited for its answer, coarsely. A clock-skewed future `created_at` is `lt_1h`."""
    age = (now or timezone.now()) - created_at
    for limit, label in _AGE_BUCKETS:
        if age < limit:
            return label
    return "7d_plus"


def request_decided(decider, kind, outcome, created_at):
    """`shelter_request_decided` when a SHELTER answers an inbox request (`kind` is the inbox's: adoption,
    volunteer or placement). A person deciding on their own listing is not a shelter event. Sent on commit,
    so a decision that rolls back is never counted. `decider=None` is the system (the lapsed sweep)."""
    if decider is not None and decider.account_type != "shelter":
        return
    bucket = age_bucket(created_at)
    transaction.on_commit(lambda: emit("shelter_request_decided", kind=kind, outcome=outcome, age_bucket=bucket))


def emit_profile_created(profile):
    emit("shelter_profile_created", tier=profile.tier, org_type=profile.org_type)


def emit_profile_updated(fields):
    """Names only, never values; nothing when nothing changed."""
    if fields:
        emit("shelter_profile_updated", fields=list(fields))
