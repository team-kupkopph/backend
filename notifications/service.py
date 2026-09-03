"""Write side of notifications (US-X1).

`notify()` is the one place a notification row is created, so every feature that tells a
user something goes through the same door. In-app only this sprint; Sprint 5 (E10) adds
push on top of these rows.
"""
import logging

from django.db import transaction

from notifications.models import Notification
from notifications.push import send_push
from notifications.types import REGISTRY, is_registered

logger = logging.getLogger("kupkop.push")


class UnregisteredNotificationType(Exception):
    """US-N1 · `type` isn't in `notifications.types.REGISTRY`. Raised at the one write
    door rather than left to silently ship a notification no client knows how to route —
    register the type (with its `data` shape) instead of catching this."""


def _fan_out_push(account, type, title, body, data):
    """US-P2 · post-commit push fan-out for `notify()`. Runs only after the transaction
    that created the in-app row commits — a rolled-back transaction sends nothing. Every
    exception here is caught and logged, never propagated: a push failure must not break
    the request that called `notify()`."""
    try:
        t = REGISTRY.get(type)
        if t is None or not t.push:
            return
        settings_row = getattr(account, "settings", None)
        if settings_row is not None and not settings_row.push_enabled:
            return
        from devices.models import DeviceToken
        for tok in DeviceToken.objects.filter(account=account):
            try:
                res = send_push(tok.fcm_token, title=title, body=body, data=data)
                if res.unregistered:
                    tok.delete()
            except Exception:
                logger.exception("push send failed for one token")   # one bad token ≠ stop the rest
    except Exception:
        logger.exception("push fan-out failed")   # never propagate to the caller


def notify(account, type, *, title="", body="", data=None):
    """Create an unread in-app notification for `account`. `type` must be a key in
    `notifications.types.REGISTRY`; `data` is the deep-link payload the client routes on."""
    if not is_registered(type):
        raise UnregisteredNotificationType(type)
    row = Notification.objects.create(
        account=account, type=type, title=title, body=body, data=data)
    # TODO(FCM-enable): the push fan-out runs inline in the triggering request's commit path.
    # That is fine while send_push is a creds-guarded no-op, but once real FCM is configured it
    # adds per-device network latency to the request that called notify(). Move _fan_out_push
    # onto a task queue at FCM-enable time (US-D1). Recorded in the Sprint 5 review.
    transaction.on_commit(lambda: _fan_out_push(account, type, title, body, data))
    return row
