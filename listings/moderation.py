"""AD7 / AQ7 · what a moderation takedown does to an adoption listing (called by
moderation.actions.resolve_flag when a `listing` flag is actioned), and the staff restore that
undoes a mistaken one. The listing counterpart of sagip/moderation.py.

Hide, never delete: the row, its photos and its inquiries are the audit trail."""
from django.db import transaction
from django.utils import timezone

from listings.models import AdoptionInquiry, EndReason, InquiryStatus
from notifications.service import notify
from sagip import notices


class ListingNotHidden(Exception):
    """`restore_listing` on a listing no takedown is holding."""


@transaction.atomic
def hide_listing(listing, by):
    """Hide `listing` and close the inquiries a takedown strands.

    Every open inquiry closes as `listing_withdrawn` (ended by the system, so no account is
    named) and its adopter is told, EXCEPT the one the animal is reserved for: AQ7, a takedown
    never reverses an adoption that is already under way. The poster is told it was removed.

    Lock order is R1's (the report's case, the report, the listing, then the inquiries by pk), the
    one every handoff writer takes, so a takedown racing an accept, a reserve or a take-back
    queues instead of deadlocking."""
    from listings.views import _lock_handoff  # deferred: views imports this package's models

    peek = (type(listing).objects.filter(pk=listing.pk).values("source_report_id").first())
    if peek is None:
        return
    locked = _lock_handoff(listing.pk, peek["source_report_id"])
    if locked is None or locked.hidden_at is not None:
        return
    now = timezone.now()
    locked.hidden_at = now
    locked.save(update_fields=["hidden_at", "updated_at"])
    open_ones = list(AdoptionInquiry.objects.select_for_update()
                     .filter(listing=locked, status=InquiryStatus.ACTIVE, reserved_at__isnull=True)
                     .select_related("adopter_account").order_by("pk"))
    for inquiry in open_ones:
        inquiry.listing = locked
        inquiry.status, inquiry.decided_at = InquiryStatus.WITHDRAWN, now
        inquiry.ended_by_account, inquiry.end_reason = None, EndReason.LISTING_WITHDRAWN
        inquiry.save(update_fields=["status", "decided_at", "ended_by_account", "end_reason",
                                    "updated_at"])
        notices.listing_withdrawn(locked, inquiry)
    notify(locked.posted_by, "listing_removed", title="A listing of yours was removed",
           body=f"Our moderators removed {locked.name or 'your listing'}, so it's no longer on Kupkop.",
           data={"listing_id": str(locked.pk)})
    listing.hidden_at = now


@transaction.atomic
def restore_listing(listing, by, reason):
    """U1-style staff restore: clears `hidden_at` and nothing else. The inquiries the takedown
    closed stay closed (their adopters were told it was gone) and nobody is notified; the listing
    is simply on the feed again if it is still `available` and its poster still qualifies.
    Raises `ListingNotHidden` when there is nothing to restore."""
    from listings.models import AdoptionListing
    locked = AdoptionListing.objects.select_for_update().get(pk=listing.pk)
    if locked.hidden_at is None:
        raise ListingNotHidden()
    locked.hidden_at = None
    locked.save(update_fields=["hidden_at", "updated_at"])
    listing.hidden_at = None
    return listing
