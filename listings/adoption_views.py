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

from common.analytics import emit
from listings import notices
from listings.models import (
    REJECT_REASONS,
    AdoptionInquiry,
    AdoptionListing,
    AdoptionStageKey,
    EndReason,
    InquiryKind,
    InquiryStatus,
    ListingStatus,
    Pet,
    PetRemovedReason,
    StageState,
)
from listings.representations import poster_contact_phone, poster_inquiry_rows
from listings.stages import set_stage_state
from listings.views import (
    _lock_handoff,
    _locked_inquiry,
    _no_such_inquiry,
    _pet_from_listing,
    _take_back,
)
from listings.visibility import account_is_verified_member
from sagip import notices as sagip_notices
from sagip.models import StrayStatus
from sagip.status import resolve_report


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
                # §5.3 · a re-Reserve drops "Reservation released".
                stage.note = ""
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


class CompleteView(APIView):
    """AD5 / AQ3 · POST /inquiries/{id}/complete — the adoption happened. Only for the applicant
    the animal is reserved for, and only while they still hold the badge (AQ2; re-checked, since a
    badge can be revoked between Reserve and Complete). In one transaction:
    - the inquiry is adopted, and its finalization step is done;
    - the adopter gets the Pet;
    - the listing is adopted;
    - every other open applicant is declined as "another adopter was chosen" and told;
    - a rescue-sourced report is resolved (the D4 remainder);
    - the poster's badges are reconciled.
    Lock order: R1's case → report → listing, then this inquiry, then the others by pk."""
    permission_classes = [IsAuthenticated]

    def post(self, request, inquiry_id):
        from community.badges import award_badges_for  # deferred: avoids an import cycle

        with transaction.atomic():
            listing, inquiry = _locked_inquiry(inquiry_id)
            if inquiry is None:
                return _no_such_inquiry()
            refusal = _poster_gate(inquiry, request.user)
            if refusal:
                return refusal
            if inquiry.reserved_at is None:
                return _err("not_reserved", "Reserve the animal for them first", 409)
            adopter = inquiry.adopter_account
            if not account_is_verified_member(adopter):
                return _err("adopter_badge_required",
                            "They need a Verified Member badge to complete the adoption", 409)
            others = list(AdoptionInquiry.objects.select_for_update()
                          .filter(listing=listing, status=InquiryStatus.ACTIVE)
                          .exclude(pk=inquiry.pk).order_by("pk"))
            now = timezone.now()
            set_stage_state(inquiry.stages.get(stage_key=AdoptionStageKey.FINALIZATION),
                            StageState.DONE, request.user)
            inquiry.status, inquiry.decided_at, inquiry.ended_by_account = (
                InquiryStatus.ADOPTED, now, request.user)
            inquiry.save(update_fields=["status", "decided_at", "ended_by_account", "updated_at"])
            pet = _pet_from_listing(listing, adopter)
            listing.status, listing.adopted_pet, listing.adopted_by_account = (
                ListingStatus.ADOPTED, pet, adopter)
            listing.save(update_fields=["status", "adopted_pet", "adopted_by_account", "updated_at"])
            for other in others:
                other.listing = listing
                _end(other, InquiryStatus.DECLINED, request.user, EndReason.ANOTHER_ADOPTER_CHOSEN, now)
                notices.inquiry_rejected(other)
            if listing.source_report_id is not None:
                report = listing.source_report
                if resolve_report(report, request.user, note="adopted through Kupkop"):
                    case = report.cases.filter(expired_at__isnull=True).first()
                    if case is not None:
                        sagip_notices.case_progress(report, case, StrayStatus.RESOLVED)
            notices.adoption_completed(inquiry, pet)
            award_badges_for(listing.posted_by)
            emit("inquiry_decided", outcome="adopted")
            emit("adoption_completed")
        return Response({"inquiry_id": str(inquiry.pk), "listing_id": str(listing.pk),
                         "pet_id": str(pet.pk)})


def _locked_own_listing(listing_id, user):
    """(listing, None) locked in R1 order for its poster, else (None, error response). Call inside
    transaction.atomic()."""
    peek = (AdoptionListing.objects.filter(pk=listing_id).values("source_report_id").first())
    if peek is None:
        return None, _err("not_found", "No such listing", 404)
    listing = _lock_handoff(listing_id, peek["source_report_id"])
    if listing is None:
        return None, _err("not_found", "No such listing", 404)
    if listing.posted_by_id != user.pk:
        return None, _err("not_your_listing", "Only the poster can do this", 403)
    return listing, None


class WithdrawListingView(APIView):
    """AD10 / D15 · POST /listings/{id}/withdraw {"close_inquiries"?} — the poster takes their own
    listing off Kupkop: a draft, a live listing, one that's reserved, or an unanswered placement.
    People who asked are never closed on silently: without {"close_inquiries": true} the poster
    is told how many, and nothing changes. The rescuer's POST /cases/{id}/handoff/cancel runs
    the same code."""
    permission_classes = [IsAuthenticated]

    def post(self, request, listing_id):
        with transaction.atomic():
            listing, error = _locked_own_listing(listing_id, request.user)
            if error:
                return error
            if listing.status == ListingStatus.WITHDRAWN:
                return _err("not_withdrawable", "This listing is already withdrawn", 409)
            return _take_back(listing, request.user, request.data)


class MarkAdoptedView(APIView):
    """AD10 · POST /listings/{id}/mark-adopted — the animal found a home outside Kupkop. The
    listing reads Adopted, with no adopter account and no Pet (there is no one to give one to);
    every open applicant is declined as "another adopter was chosen" and told; a rescue report is
    resolved. If an applicant holds the reservation the poster completes it with them instead,
    so the adoption lands in that person's My Pets. A rehomed pet is archived out of the poster's
    My Pets (PP2)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, listing_id):
        from community.badges import award_badges_for  # deferred: avoids an import cycle

        with transaction.atomic():
            listing, error = _locked_own_listing(listing_id, request.user)
            if error:
                return error
            if listing.status not in (ListingStatus.AVAILABLE, ListingStatus.PENDING):
                return _err("not_markable", "Only a listing that's on the feed or reserved can be "
                                            "marked adopted", 409)
            active = list(AdoptionInquiry.objects.select_for_update()
                          .filter(listing=listing, status=InquiryStatus.ACTIVE).order_by("pk"))
            if any(i.reserved_at is not None for i in active):
                return _err("use_complete", "Someone holds the reservation. Complete the adoption "
                                            "with them instead.", 409)
            if any(i.kind == InquiryKind.PLACEMENT for i in active):
                return _err("placement_pending", "This animal is offered to someone. Withdraw the "
                                                 "offer first.", 409)
            now = timezone.now()
            listing.status = ListingStatus.ADOPTED
            listing.save(update_fields=["status", "updated_at"])
            # PP2 / PQ1 · a pet rehomed outside Kupkop has left its owner: archive it out of My Pets
            # (locked after the listing, the order _pet_from_listing uses). Kept, never deleted.
            if listing.rehome_pet_id is not None:
                Pet.objects.select_for_update().filter(
                    pk=listing.rehome_pet_id, owner_account=listing.posted_by_id,
                    removed_at__isnull=True,
                ).update(removed_at=now, removed_reason=PetRemovedReason.REHOMED_OUTSIDE, updated_at=now)
            for other in active:
                other.listing = listing
                _end(other, InquiryStatus.DECLINED, request.user,
                     EndReason.ANOTHER_ADOPTER_CHOSEN, now)
                notices.inquiry_rejected(other)
            if listing.source_report_id is not None:
                report = listing.source_report
                if resolve_report(report, request.user, note="adopted outside Kupkop"):
                    case = report.cases.filter(expired_at__isnull=True).first()
                    if case is not None:
                        sagip_notices.case_progress(report, case, StrayStatus.RESOLVED)
            award_badges_for(listing.posted_by)
            emit("adoption_completed")
        return Response({"status": "adopted", "closed_inquiries": len(active)})
