"""C14 / D11 · an unanswered direct placement doesn't hold an animal's handoff forever."""
from django.db import transaction
from django.utils import timezone

from listings import notices as adoption_notices
from listings.models import AdoptionInquiry, EndReason, InquiryKind, InquiryStatus, ListingStatus
from sagip import notices

PLACEMENT_EXPIRY_DAYS = 7   # D11 · a policy number — move it deliberately
STALE_NUDGE_DAYS = 7        # AD23 · an unanswered public inquiry nudges its poster...
STALE_CLOSE_DAYS = 21       # ...and closes politely after three weeks. Policy numbers, as above.


def expire_placements(now=None):
    """D11 · withdraw every direct placement left unanswered for PLACEMENT_EXPIRY_DAYS.

    R1 · each row is locked in the one order every handoff writer takes — the report's active
    case -> the report -> the listing -> the inquiry, as in PlacementDecisionView and
    CaseHandoffCancelView (and hide_report's case -> report) — so the sweep racing an accept, a
    take-back or a takedown queues instead of deadlocking. Everything that made a row a
    candidate (a placement, ACTIVE, PENDING) is re-checked under those locks."""
    from listings.views import _lock_handoff, _withdraw_placement
    now = now or timezone.now()
    cutoff = now - timezone.timedelta(days=PLACEMENT_EXPIRY_DAYS)
    candidates = (AdoptionInquiry.objects
                  .filter(kind=InquiryKind.PLACEMENT, status=InquiryStatus.ACTIVE,
                          listing__status=ListingStatus.PENDING, created_at__lte=cutoff)
                  .values_list("pk", "listing_id", "listing__source_report_id"))
    expired = []
    for pk, listing_id, source_report_id in candidates:
        with transaction.atomic():
            listing = _lock_handoff(listing_id, source_report_id)
            inq = (AdoptionInquiry.objects.select_for_update()
                   .filter(pk=pk, status=InquiryStatus.ACTIVE).first())
            if inq is None or listing is None or listing.status != ListingStatus.PENDING:
                continue
            inq.listing = listing
            if inq.kind != InquiryKind.PLACEMENT:     # AD22 · a public inquiry, not a placement
                continue
            _withdraw_placement(inq, now, reason="expired")
            notices.placement_decided(inq.listing, inq, "expired")
            expired.append(inq)
    return expired


def _stale_candidates(cutoff):
    """AD23 · public inquiries the poster has done nothing with: still ACTIVE, not accepted for
    screening, not the reserved one, on a listing that is live (a takedown or a withdraw has already
    closed the others). A placement is D11's, not this."""
    return (AdoptionInquiry.objects
            .filter(kind=InquiryKind.INQUIRY, status=InquiryStatus.ACTIVE, accepted_at__isnull=True,
                    reserved_at__isnull=True, created_at__lte=cutoff,
                    listing__status__in=(ListingStatus.AVAILABLE, ListingStatus.PENDING))
            .values_list("pk", "listing_id", "listing__source_report_id"))


def _locked_stale(pk, listing_id, source_report_id, cutoff):
    """The candidate re-read under R1's lock order, or None if it stopped being one."""
    from listings.views import _lock_handoff
    listing = _lock_handoff(listing_id, source_report_id)
    inq = (AdoptionInquiry.objects.select_for_update()
           .select_related("adopter_account", "listing__posted_by")
           .filter(pk=pk, kind=InquiryKind.INQUIRY, status=InquiryStatus.ACTIVE,
                   accepted_at__isnull=True, reserved_at__isnull=True,
                   created_at__lte=cutoff).first())
    if inq is None or listing is None:
        return None
    inq.listing = listing
    return inq


def nudge_stale_inquiries(now=None):
    """AD23 · tell the poster, once, about an inquiry that has waited STALE_NUDGE_DAYS."""
    now = now or timezone.now()
    cutoff = now - timezone.timedelta(days=STALE_NUDGE_DAYS)
    nudged = []
    for pk, listing_id, source_report_id in _stale_candidates(cutoff).filter(nudged_at__isnull=True):
        with transaction.atomic():
            inq = _locked_stale(pk, listing_id, source_report_id, cutoff)
            if inq is None or inq.nudged_at is not None:
                continue
            inq.nudged_at = now
            inq.save(update_fields=["nudged_at"])
            adoption_notices.inquiry_waiting(inq)
            nudged.append(inq)
    return nudged


def close_stale_inquiries(now=None):
    """AD23 · close an inquiry nobody answered in STALE_CLOSE_DAYS: withdrawn, by no one, with a
    polite notice to the adopter. If it was somehow the animal's only hold, nothing else moves."""
    from listings.adoption_views import _end
    now = now or timezone.now()
    cutoff = now - timezone.timedelta(days=STALE_CLOSE_DAYS)
    closed = []
    for pk, listing_id, source_report_id in _stale_candidates(cutoff):
        with transaction.atomic():
            inq = _locked_stale(pk, listing_id, source_report_id, cutoff)
            if inq is None:
                continue
            _end(inq, InquiryStatus.WITHDRAWN, None, EndReason.INQUIRY_EXPIRED, now)
            adoption_notices.inquiry_rejected(inq)
            closed.append(inq)
    return closed
