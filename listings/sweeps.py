"""C14 / D11 · an unanswered direct placement doesn't hold an animal's handoff forever."""
from django.db import transaction
from django.utils import timezone

from listings.models import AdoptionInquiry, AdoptionStage, InquiryStatus, ListingStatus, StageState
from sagip import notices

PLACEMENT_EXPIRY_DAYS = 7   # D11 · a policy number — move it deliberately


def expire_placements(now=None):
    now = now or timezone.now()
    cutoff = now - timezone.timedelta(days=PLACEMENT_EXPIRY_DAYS)
    candidates = (AdoptionInquiry.objects
                  .filter(status=InquiryStatus.ACTIVE, listing__status=ListingStatus.PENDING,
                          created_at__lte=cutoff)
                  .values_list("pk", flat=True))
    expired = []
    for pk in candidates:
        with transaction.atomic():
            inq = (AdoptionInquiry.objects.select_for_update().select_related("listing")
                   .filter(pk=pk, status=InquiryStatus.ACTIVE).first())
            if inq is None:
                continue
            states = set(AdoptionStage.objects.filter(inquiry=inq).values_list("state", flat=True))
            if states != {StageState.SKIPPED}:     # a public inquiry, not a placement
                continue
            from listings.views import _withdraw_placement
            _withdraw_placement(inq, now)
            notices.placement_decided(inq.listing, inq, "expired")
            expired.append(inq)
    return expired
