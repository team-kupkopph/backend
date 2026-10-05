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
from listings.models import (
    REJECT_REASONS,
    AdoptionInquiry,
    AdoptionStageKey,
    EndReason,
    InquiryKind,
    InquiryStatus,
    ListingStatus,
    StageState,
)
from listings.representations import poster_contact_phone, poster_inquiry_rows
from listings.stages import set_stage_state
from listings.views import _locked_inquiry, _no_such_inquiry
from listings.visibility import account_is_verified_member


def _err(code, message, status):
    return Response({"error": {"code": code, "message": message}}, status=status)


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


def _end(inquiry, status, by, reason, now=None):
    """Close one inquiry with who and why (AD6). If the animal was reserved for it, the listing
    goes back on the feed (AQ4). Returns True when that happened. Call on locked rows."""
    now = now or timezone.now()
    was_reserved = inquiry.reserved_at is not None
    inquiry.status, inquiry.decided_at = status, now
    inquiry.ended_by_account, inquiry.end_reason, inquiry.reserved_at = by, reason, None
    inquiry.save(update_fields=["status", "decided_at", "ended_by_account", "end_reason",
                                "reserved_at", "updated_at"])
    reopened = was_reserved and inquiry.listing.status == ListingStatus.PENDING
    if reopened:
        inquiry.listing.status = ListingStatus.AVAILABLE
        inquiry.listing.save(update_fields=["status", "updated_at"])
    return reopened


class RejectView(APIView):
    """AD6 · POST /inquiries/{id}/reject {"reason"} — the poster turns an applicant down, with one
    of four reasons the adopter is shown. Turning down the applicant the animal is reserved for
    puts it back on the feed."""
    permission_classes = [IsAuthenticated]

    def post(self, request, inquiry_id):
        body = request.data if isinstance(request.data, dict) else {}
        with transaction.atomic():
            listing, inquiry = _locked_inquiry(inquiry_id)
            if inquiry is None:
                return _no_such_inquiry()
            refusal = _poster_gate(inquiry, request.user)
            if refusal:
                return refusal
            if body.get("reason") not in REJECT_REASONS:
                return _err("bad_reason", "Choose a reason: not_a_fit, requirements_not_met, "
                                          "no_response or other", 422)
            _end(inquiry, InquiryStatus.DECLINED, request.user, body["reason"])
            notices.inquiry_rejected(inquiry)
        return Response({"status": inquiry.status, "end_reason": inquiry.end_reason})


class WithdrawView(APIView):
    """AD6 · POST /inquiries/{id}/withdraw — the adopter steps back from a public inquiry. A
    placement's recipient answers with /decline instead."""
    permission_classes = [IsAuthenticated]

    def post(self, request, inquiry_id):
        with transaction.atomic():
            listing, inquiry = _locked_inquiry(inquiry_id)
            if inquiry is None:
                return _no_such_inquiry()
            if inquiry.adopter_account_id != request.user.pk:
                return _err("not_your_inquiry", "Only the person who asked can withdraw", 403)
            if inquiry.kind != InquiryKind.INQUIRY:
                return _err("is_placement", "Decline the placement instead", 409)
            if inquiry.status != InquiryStatus.ACTIVE:
                return _err("inquiry_closed", "This inquiry is already closed", 409)
            reopened = _end(inquiry, InquiryStatus.WITHDRAWN, request.user,
                            EndReason.ADOPTER_WITHDREW)
            notices.inquiry_withdrawn(inquiry, reopened)
        return Response({"status": inquiry.status, "end_reason": inquiry.end_reason})


class ReserveView(APIView):
    """AQ4 · POST /inquiries/{id}/reserve — the poster holds the animal for one applicant they have
    screened. The listing reads Reserved (`pending`) and takes no new inquiries. The other
    applicants keep waiting; they aren't declined. AQ2 · the adopter must hold the Verified Member
    badge by now. If they don't, the poster is refused and the adopter is told what's missing."""
    permission_classes = [IsAuthenticated]

    def post(self, request, inquiry_id):
        with transaction.atomic():
            listing, inquiry = _locked_inquiry(inquiry_id)
            if inquiry is None:
                return _no_such_inquiry()
            refusal = _poster_gate(inquiry, request.user)
            if refusal:
                return refusal
            if inquiry.accepted_at is None:
                return _err("not_screening", "Accept this applicant for screening first", 409)
            if inquiry.reserved_at is not None:
                return _err("already_reserved", "This animal is already reserved for them", 409)
            if listing.status != ListingStatus.AVAILABLE:
                if listing.status == ListingStatus.PENDING:
                    return _err("reserved_for_another", "This animal is reserved for someone else", 409)
                return _err("listing_unavailable", "This animal is no longer listed", 409)
            if not account_is_verified_member(inquiry.adopter_account):
                notices.adoption_badge_needed(inquiry)
                return _err("adopter_badge_required",
                            "They need a Verified Member badge before you can reserve. We've told them.",
                            409)
            inquiry.reserved_at = timezone.now()
            inquiry.save(update_fields=["reserved_at", "updated_at"])
            listing.status = ListingStatus.PENDING
            listing.save(update_fields=["status", "updated_at"])
            stage = inquiry.stages.get(stage_key=AdoptionStageKey.FINALIZATION)
            if stage.state == StageState.NOT_STARTED:
                set_stage_state(stage, StageState.IN_PROGRESS, request.user)
            notices.adoption_reserved(inquiry)
        return Response(_poster_row(inquiry))


class UnreserveView(APIView):
    """AQ4 · POST /inquiries/{id}/unreserve — it fell through for now. The applicant stays in the
    running, the listing goes back on the feed, and the others carry on."""
    permission_classes = [IsAuthenticated]

    def post(self, request, inquiry_id):
        with transaction.atomic():
            listing, inquiry = _locked_inquiry(inquiry_id)
            if inquiry is None:
                return _no_such_inquiry()
            refusal = _poster_gate(inquiry, request.user)
            if refusal:
                return refusal
            if inquiry.reserved_at is None:
                return _err("not_reserved", "This animal isn't reserved for them", 409)
            inquiry.reserved_at = None
            inquiry.save(update_fields=["reserved_at", "updated_at"])
            if listing.status == ListingStatus.PENDING:
                listing.status = ListingStatus.AVAILABLE
                listing.save(update_fields=["status", "updated_at"])
            stage = inquiry.stages.get(stage_key=AdoptionStageKey.FINALIZATION)
            if stage.state == StageState.IN_PROGRESS:
                set_stage_state(stage, StageState.NOT_STARTED, request.user, note="Reservation released")
            notices.reservation_released(inquiry)
        return Response(_poster_row(inquiry))
