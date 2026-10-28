"""Notices told to an account's own addresses about something that happened to it.

Best effort by construction: the action has already succeeded, and a mail outage must not turn a
completed change into an error (the person would retry, and the second attempt would find nothing
to do). A failure is logged and swallowed here, in one place, so no caller has to remember to.
"""
import logging

from common.messages import compose_email_changed_notice
from common.senders import get_sender

logger = logging.getLogger("kupkop.notices")


def notify_email_changed(old_address, new_address):
    """Tell the OLD address its account now uses `new_address` (T13). Never raises."""
    try:
        subject, body = compose_email_changed_notice(new_address)
        get_sender().send_notice(to=old_address, subject=subject, body=body)
    except Exception:
        logger.warning({"event": "email_changed_notice_failed", "to": (old_address or "")[:2] + "***"},
                       exc_info=True)
