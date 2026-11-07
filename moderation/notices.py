"""Review steps 22 and 7h (MQ7) · what a reporter and a poster are told about a decision.

Apple 1.2 asks for "timely responses to concerns"; a report that vanishes into a queue is the opposite. A reporter is told when
their report has been looked at. They are NOT told what was decided: an outcome is the subject's business, a reporter who learns
"nothing was done" is invited to argue it, and one who learns "it was removed" is told something about someone else. MQ7 settled it:
"we reviewed it", nothing more.

A poster whose thing is taken down is told, and told how to ask for a second look: a plain address, not a form (MQ7: "appeal by a
contact link in the takedown notice for launch").
"""
import logging

from django.conf import settings

from notifications.service import notify

logger = logging.getLogger("kupkop.moderation")

TITLE = "We looked at your report"
BODY = ("Thank you for telling us. We review every report and act where something breaks our rules. "
        "We can't share what we decided.")


#: Review step 8 (MQ3 categories) · what a poster is told about WHY, by the category the moderator chose. A fixed sentence per
#: category on purpose: it can never carry the reporter's words, the moderator's internal note, or anything about who reported.
REMOVAL_REASONS = {
    "spam_scam": "It looked like spam or a scam.",
    "animal_welfare": "It put an animal's welfare at risk.",
    "harassment": "It contained harassment or abuse.",
    "misleading": "It was misleading or fake.",
    "other": "It broke our community rules.",
}


def removal_reason(code):
    """The sentence for a poster, or "" for a code we do not know (a notice with no reason beats one with a wrong one)."""
    return REMOVAL_REASONS.get(code, "")


def appeal_line():
    """The sentence added to a takedown notice. One place, so every notice offers the same way back."""
    return f"If you think this was a mistake, write to {settings.SUPPORT_EMAIL}."


def tell_reporters(flags):
    """Tell each flag's reporter it was looked at. A system flag has no reporter, and a deleted reporter has nobody to tell.
    Never raises: a notice that fails must not undo the decision it reports."""
    for flag in flags:
        reporter = flag.reporter_account
        if reporter is None:
            continue
        try:
            notify(reporter, "report_reviewed", title=TITLE, body=BODY, data={"flag_id": str(flag.pk)})
        except Exception:
            logger.exception("could not tell a reporter their report was reviewed")
