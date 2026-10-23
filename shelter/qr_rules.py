"""DW2 · small shared rules for the donation-QR lifecycle (the shelter's endpoints and the reviewer's both use them)."""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from common.storage import PUBLIC, delete_object

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


MAX_FILE_URL = 500


def donation_qr_url_error(url, account):
    """AT10 · `file_url` is stored and later rendered to donors and in the reviewer's browser, so it must be THIS account's own `donation_qr`
    upload: the object key the presign endpoint hands out is `donation_qr/{account_id}/{uuid}` (`verifications/views.py::PresignView`). Returns an
    error string, or None when the URL is acceptable. Plain http is allowed only in DEBUG (the local dev receiver); with a public bucket configured
    the host must be that bucket."""
    from urllib.parse import urlsplit

    from django.conf import settings

    if not isinstance(url, str) or len(url) > MAX_FILE_URL:
        return "That image link is not valid."
    try:
        parts = urlsplit(url)
        port = parts.port  # noqa: F841 — raises on a malformed port
    except ValueError:
        return "That image link is not valid."
    https_ok = parts.scheme == "https" or (settings.DEBUG and parts.scheme == "http")
    if not https_ok or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        return "That image link is not valid."
    bucket = getattr(settings, "MEDIA_S3_BUCKET_PUBLIC", "")
    if bucket and parts.hostname != bucket:
        return "That image was not uploaded through Kupkop."
    segments = [seg for seg in parts.path.split("/") if seg]
    if ".." in segments or "." in segments:
        return "That image link is not valid."
    # .../donation_qr/{account_id}/{object}: the purpose and the owner are part of the key, so another account's (or another purpose's) upload fails.
    for i in range(len(segments) - 2):
        if segments[i] == "donation_qr" and segments[i + 1] == str(account.pk) and len(segments) == i + 3:
            return None
    return "That image was not uploaded for this account."


def discard_image(qr):
    """AT21 · a withdrawn or superseded QR's image must not stay reachable at its old public URL (the bucket is world-readable). Deletes the stored
    object once the surrounding transaction commits, and ONLY when the URL is this account's own `donation_qr` key (never a legacy foreign URL).
    A refused image is deliberately kept: the reviewer's record and the refused-image rule refer to it. No bucket (dev) makes this a no-op."""
    if donation_qr_url_error(qr.qr_image_url, qr.account) is not None:
        return
    url = qr.qr_image_url
    transaction.on_commit(lambda: delete_object(url, PUBLIC))


def swap_in(pending):
    """Make `pending` the live QR and withdraw (and discard the image of) whatever was live for that provider, in the caller's transaction."""
    from shelter.models import DonationQr, QrStatus
    for old in DonationQr.objects.select_for_update().filter(
            account=pending.account, provider=pending.provider, status=QrStatus.LIVE).exclude(pk=pending.pk):
        old.status = QrStatus.WITHDRAWN
        old.decided_at = timezone.now()
        old.save()
        discard_image(old)
    pending.status = QrStatus.LIVE
