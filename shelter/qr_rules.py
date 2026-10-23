"""DW2 · small shared rules for the donation-QR lifecycle (the shelter's endpoints and the reviewer's both use them)."""
from datetime import timedelta

from django.utils import timezone

OVERDUE_BUSINESS_DAYS = 2   # owner decision, DW2 plan Q2: past this a pending QR is flagged Overdue (never auto-approved)


def business_days_elapsed(start, end=None):
    """Whole Monday-Friday days between `start` and `end` (now). Public holidays are not modelled: a flag, not a deadline."""
    end = end or timezone.now()
    days, cursor = 0, start
    while cursor.date() < end.date():
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def is_overdue(qr):
    from shelter.models import QrStatus
    return qr.status == QrStatus.PENDING and business_days_elapsed(qr.created_at) > OVERDUE_BUSINESS_DAYS


def own_repr(qr):
    """What a shelter sees of its OWN row (the public endpoint never carries any of this)."""
    return {"donation_qr_id": str(qr.pk), "provider": qr.provider, "account_name": qr.account_name,
            "qr_image_url": qr.qr_image_url, "status": qr.status, "verified": qr.verified,
            "replaces": str(qr.replaces_id) if qr.replaces_id else None,
            "decision_note": qr.decision_note, "created_at": qr.created_at.isoformat(),
            "decided_at": qr.decided_at.isoformat() if qr.decided_at else None, "overdue": is_overdue(qr)}


def notify_decision(qr, decision, note=""):
    """Tell the shelter what the reviewer decided (closes AT11). `decision` is verified | refused | taken_down."""
    from notifications.service import notify
    title = {"verified": "Your donation QR is live", "refused": "Your replacement QR was not approved",
             "taken_down": "Your donation QR was taken down"}[decision]
    body = {"verified": "Donors can now see this QR.",
            "refused": "Your current QR stays live. " + (note or ""),
            "taken_down": "Donors can no longer see it. " + (note or "")}[decision].strip()
    notify(qr.account, "donation_qr_decided", title=title, body=body,
           data={"donation_qr_id": str(qr.pk), "provider": qr.provider, "decision": decision})
