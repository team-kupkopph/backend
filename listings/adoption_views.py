"""The poster's half of a public adoption, and the adopter's withdraw
(dev/adoption-build-review.md AD1–AD6; AQ1–AQ5, decided 2026-10-05).

Every write locks in R1's one order (the report's active case, the report, the listing, then the
inquiry) through listings.views._lock_handoff, and re-checks on the locked rows. A poster's tap
that races a take-back, a takedown or the adopter's own withdraw then queues instead of
deadlocking."""
from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from listings import notices
from listings.models import AdoptionInquiry, AdoptionStageKey, InquiryKind, InquiryStatus, StageState
from listings.representations import poster_contact_phone, poster_inquiry_rows
from listings.stages import set_stage_state
from listings.views import _lock_handoff


def _err(code, message, status):
    return Response({"error": {"code": code, "message": message}}, status=status)


def _no_such_inquiry():
    return _err("not_found", "No such inquiry", 404)


def _locked_inquiry(inquiry_id):
    """(listing, inquiry), locked in R1 order, or (None, None) if either is gone.
    Call inside transaction.atomic(). No select_related on the locked read: a join would lock the
    adopter's account row too."""
    peek = (AdoptionInquiry.objects.filter(pk=inquiry_id)
            .values("listing_id", "listing__source_report_id").first())
    if peek is None:
        return None, None
    listing = _lock_handoff(peek["listing_id"], peek["listing__source_report_id"])
    inquiry = AdoptionInquiry.objects.select_for_update().filter(pk=inquiry_id).first()
    if listing is None or inquiry is None:
        return None, None
    inquiry.listing = listing
    return listing, inquiry


def _poster_gate(inquiry, user):
    """What every poster action checks, on locked rows: the caller posted the listing, it's a
    public inquiry (a placement is answered by its recipient), and it's still open."""
    if inquiry.listing.posted_by_id != user.pk:
        return _err("not_your_listing", "Only the poster can do this", 403)
    if inquiry.kind != InquiryKind.INQUIRY:
        return _err("is_placement", "A placement is answered by the person it was offered to", 409)
    if inquiry.status != InquiryStatus.ACTIVE:
        return _err("inquiry_closed", "This inquiry is already closed", 409)
    return None


def _poster_row(inquiry):
    fresh = (AdoptionInquiry.objects.select_related("listing", "listing__posted_by", "adopter_account")
             .prefetch_related("stages").get(pk=inquiry.pk))
    return poster_inquiry_rows([fresh])[0]


class ScreenView(APIView):
    """AQ1 / AD3 · POST /inquiries/{id}/screen — the poster accepts an applicant for screening.
    From here on both phones are shared, the application stage is in progress, and the adopter
    has been told. The poster must have a number to share (`poster_phone_required`), so the
    exchange is never one-sided."""
    permission_classes = [IsAuthenticated]

    def post(self, request, inquiry_id):
        with transaction.atomic():
            listing, inquiry = _locked_inquiry(inquiry_id)
            if inquiry is None:
                return _no_such_inquiry()
            refusal = _poster_gate(inquiry, request.user)
            if refusal:
                return refusal
            if inquiry.accepted_at is not None:
                return _err("already_screening", "You already accepted this applicant", 409)
            if poster_contact_phone(request.user) is None:
                return _err("poster_phone_required",
                            "Add and verify a phone number first, so the adopter can reach you", 409)
            inquiry.accepted_at = timezone.now()
            inquiry.save(update_fields=["accepted_at", "updated_at"])
            stage = inquiry.stages.get(stage_key=AdoptionStageKey.APPLICATION)
            if stage.state == StageState.NOT_STARTED:
                set_stage_state(stage, StageState.IN_PROGRESS, request.user)
            notices.inquiry_accepted(inquiry)
        return Response(_poster_row(inquiry))
