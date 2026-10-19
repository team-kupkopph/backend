"""AD20 · where an animal's handoff stands, for the claiming rescuer's case screen."""
from django.db.models import Count, Q

from listings.models import AdoptionInquiry, AdoptionListing, InquiryKind, InquiryStatus, ListingStatus

# The same four states S20 calls "the animal's live handoff"; a withdrawn listing is over.
LIVE_STATUSES = (ListingStatus.DRAFT, ListingStatus.AVAILABLE, ListingStatus.PENDING,
                 ListingStatus.ADOPTED)


def handoff_summary(report):
    """None when the animal isn't listed or offered; else `{listing_id, kind, status, open_inquiries}`
    and, for a direct placement, `recipient` (the person it is waiting on). `kind` is `placement`
    when the one active inquiry is a placement, else `listing`."""
    item = (AdoptionListing.objects.filter(source_report=report, status__in=LIVE_STATUSES)
            .annotate(open_inquiries=Count("inquiries", filter=Q(inquiries__status=InquiryStatus.ACTIVE)))
            .order_by("-created_at").first())
    if item is None:
        return None
    out = {"listing_id": str(item.pk), "kind": "listing", "status": item.status,
           "open_inquiries": item.open_inquiries}
    placement = (AdoptionInquiry.objects.select_related("adopter_account")
                 .filter(listing=item, kind=InquiryKind.PLACEMENT, status=InquiryStatus.ACTIVE).first())
    if placement is not None:
        out["kind"] = "placement"
        out["recipient"] = placement.adopter_account.display_name
        out["open_inquiries"] = 0     # a placement is one offer, not a crowd of inquiries
    return out
