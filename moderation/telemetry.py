"""Review step 14 (MO20) · what moderation tells the analytics stream.

`common/analytics.py` is the established route: one structured log line per server-authoritative outcome, with NO account id and no PII in
the payload, which is what lets it flow without consent. Moderation emitted nothing, so volumes, the dismiss rate and time to decision
could only come from ad hoc SQL. Every event here is coarse (a kind, a category, an outcome, an age bucket) and goes out after the
transaction commits, so a rolled-back decision reports nothing.
"""
from django.db import transaction

from common.analytics import emit


def age_bucket(hours):
    """How long a report waited, in a few coarse bands. An exact age is not needed and a long tail of values is not groupable."""
    if hours < 1:
        return "under_1h"
    if hours < 4:
        return "1_to_4h"
    if hours < 24:
        return "4_to_24h"
    if hours < 72:
        return "1_to_3d"
    return "over_3d"


def _later(event, **props):
    transaction.on_commit(lambda: emit(event, **props))


def flag_created(target_type, reason_code, via_person):
    _later("flag_created", target_type=target_type, reason_code=reason_code, about_a_person=bool(via_person))


def flag_folded(target_type):
    _later("flag_folded", target_type=target_type)


def flag_decided(flag, outcome, took_down, first_response):
    from django.utils import timezone
    hours = (timezone.now() - flag.created_at).total_seconds() / 3600
    _later("flag_decided", target_type=flag.target_type, outcome=outcome, reason_code=flag.decision_reason_code or None,
           took_down=bool(took_down), waited=age_bucket(hours), first_response=bool(first_response),
           system_raised=flag.reporter_account_id is None)


def restored(target_type):
    _later("moderation_restored", target_type=target_type)


def warned(reason_code):
    _later("account_warned", reason_code=reason_code)


def system_flag_raised(rule):
    _later("system_flag_raised", rule=rule)
