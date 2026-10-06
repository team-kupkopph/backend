"""C14 / D11 · an unanswered direct placement doesn't hold an animal's handoff forever."""
from django.db import transaction
from django.utils import timezone

from listings.models import AdoptionInquiry, InquiryKind, InquiryStatus, ListingStatus
from sagip import notices

PLACEMENT_EXPIRY_DAYS = 7   # D11 · a policy number — move it deliberately


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
