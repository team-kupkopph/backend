from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Account, Address
from listings.fees import fee_cap_for
from listings.models import (AdoptionInquiry, AdoptionListing, AdoptionListingPhoto,
                             AdoptionStage, AdoptionStageKey, InquiryStatus, ListingStatus,
                             Pet, PetPhoto, StageState)
from listings.permissions import IsVerifiedMember
from listings.serializers import (InquiryCreateSerializer, ListingCreateSerializer,
                                  ListingPatchSerializer, StageUpdateSerializer)
from listings.stages import set_stage_state
from listings.visibility import account_is_verified_rescuer, public_poster_q
from notifications.service import notify
from sagip.models import RescueCase, StrayStatus
from shelter.models import ShelterProfile

PAGE_SIZE = 20


def _load_safe_own_case(case_id, user):
    """H1's safe/own-case gate, shared by `CaseListView` and `CasePlaceView`: the case
    must exist, be claimed by the requesting user, and its report must be SAFE. Returns
    (case, None) on success or (None, Response) with the appropriate error status."""
    case = RescueCase.objects.select_related("report").filter(pk=case_id).first()
    if case is None:
        return None, Response({"error": {"code": "not_found", "message": "No such case"}}, status=404)
    if case.claimed_by_account_id != user.pk:
        return None, Response({"error": {"code": "not_your_case",
                                         "message": "Only the claiming rescuer can list this animal"}},
                              status=403)
    if case.report.status != StrayStatus.SAFE:
        return None, Response({"error": {"code": "case_not_safe",
                                         "message": "The animal must be safe before listing"}},
                              status=409)
    return case, None


def _paginate(qs, request):
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    start = (page - 1) * PAGE_SIZE
    items = list(qs[start:start + PAGE_SIZE + 1])
    has_next = len(items) > PAGE_SIZE
    return items[:PAGE_SIZE], (page + 1 if has_next else None)


def _pet_fields(listing):
    return {"name": listing.name, "species": listing.species, "breed": listing.breed or None,
            "sex": listing.sex or None,
            "birthdate": listing.date_of_birth.isoformat() if listing.date_of_birth else None,
            "size_category": listing.size_category or None,
            "spayed_neutered": listing.spayed_neutered,
            "vaccinated": listing.vaccinated, "walkable": listing.walkable,
            "temperament": listing.temperament or None}


def _card(listing):
    photo = listing.photos.filter(is_primary=True).first() or listing.photos.first()
    return {"listing_id": str(listing.listing_id), "pet": _pet_fields(listing),
            "city": listing.city, "status": listing.status,
            "adoption_fee": str(listing.adoption_fee),
            "photo_url": photo.url if photo else None}


def _poster_info(account):
    profile = ShelterProfile.objects.filter(account=account).first()
    addr = Address.objects.filter(account=account, is_primary=True).first()
    # account_id lets the client link to US-Q2's public donate surface
    # (GET /shelters/{account_id}/donation-qr) for a shelter poster.
    return {"account_id": str(account.pk), "name": profile.org_name if profile else account.display_name,
            "is_shelter": profile is not None, "city": addr.city if addr else None}


class ListingsView(APIView):
    """GET /listings (US-A1b, extended by US-A3) · POST /listings (US-A2).

    ⚠️ Creation is gated on `IsAuthenticated`, NOT verification. Decision 2 ("Shelter
    gating = draft-only, gated-public") — an unverified shelter (or an owner without the
    Verified Member badge) may still draft a listing; it simply never appears in `GET
    /listings`, which filters through `public_poster_q()`. Gating creation itself on
    `IsVerifiedRescuer` would have blocked exactly the drafting flow decision 2 requires —
    caught while wiring the mobile create form, before it shipped."""

    def get_permissions(self):
        return [AllowAny()] if self.request.method == "GET" else [IsAuthenticated()]

    def get(self, request):
        if request.query_params.get("mine") == "true":
            if not request.user or not request.user.is_authenticated:
                return Response({"error": {"code": "auth_required",
                                           "message": "Log in first"}}, status=401)
            qs = (AdoptionListing.objects.filter(status="available", posted_by=request.user)
                  .order_by("-created_at"))
            page_items, next_page = _paginate(qs, request)
            return Response({"results": [_card(l) for l in page_items], "next": next_page})
        qs = (AdoptionListing.objects.filter(status="available")
              .filter(public_poster_q()).distinct().order_by("-created_at"))
        city = request.query_params.get("city")
        if city:
            qs = qs.filter(city=city)
        species = request.query_params.get("species")
        if species:
            qs = qs.filter(species=species)
        page_items, next_page = _paginate(qs, request)
        return Response({"results": [_card(l) for l in page_items], "next": next_page})

    def post(self, request):
        s = ListingCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        cap = fee_cap_for(request.user)
        if cap is not None and data["adoption_fee"] > cap:
            return Response({"error": {"code": "fee_over_cap",
                                       "message": f"The adoption fee can't exceed ₱{cap}",
                                       "details": {"cap": cap}}}, status=422)
        pet = data["pet"]
        with transaction.atomic():
            listing = AdoptionListing.objects.create(
                posted_by=request.user, name=pet["name"], species=pet["species"],
                breed=pet.get("breed", ""), sex=pet.get("sex", ""),
                date_of_birth=pet.get("birthdate"), city=data["city"],
                story=data.get("description", ""), adoption_fee=data["adoption_fee"])
            for photo in data.get("photos", []):
                AdoptionListingPhoto.objects.create(listing=listing, url=photo["file_url"])
        return Response({"listing_id": str(listing.pk), "listing_status": listing.status},
                        status=201)


class CaseListView(APIView):
    """US-H1 · list an adoption from a SAFE rescue case. The listing carries source_report
    so provenance survives; the animal's species is inherited from the report. Fee capped
    by the existing fee_cap_for — no second rule."""
    permission_classes = [IsAuthenticated]

    def post(self, request, case_id):
        case, error = _load_safe_own_case(case_id, request.user)
        if error:
            return error
        fee = request.data.get("adoption_fee") or "0"
        try:
            fee_dec = Decimal(str(fee))
        except (InvalidOperation, ValueError):
            return Response({"error": {"code": "bad_request", "message": "Invalid adoption_fee"}}, status=422)
        cap = fee_cap_for(request.user)
        if cap is not None and fee_dec > cap:
            return Response({"error": {"code": "fee_over_cap",
                                       "message": f"The adoption fee can't exceed ₱{cap}",
                                       "details": {"cap": cap}}}, status=422)
        listing = AdoptionListing.objects.create(
            posted_by=request.user, source_report=case.report, species=case.report.species,
            name=request.data.get("name") or "",
            city=request.data.get("city") or case.report.city or "",
            adoption_fee=fee_dec, status=ListingStatus.AVAILABLE)
        return Response({"listing_id": str(listing.pk)}, status=201)


class CasePlaceView(APIView):
    """US-H2 · direct placement to a verified recipient — the rescuer hands a SAFE
    case's animal straight to a known Verified Member or shelter, bypassing the public
    inquiry flow. Reuses H1's safe/own-case gate. Every stage is created then immediately
    moved to SKIPPED (the placement bypass — US-H3 keys off "all stages skipped")."""
    permission_classes = [IsAuthenticated]

    def post(self, request, case_id):
        case, error = _load_safe_own_case(case_id, request.user)
        if error:
            return error
        recipient = Account.objects.filter(email=request.data.get("recipient_email")).first()
        if recipient is None:
            return Response({"error": {"code": "recipient_not_found", "message": "No such account"}}, status=404)
        if not account_is_verified_rescuer(recipient):
            return Response({"error": {"code": "recipient_not_verified",
                                       "message": "The recipient must be a verified member or shelter"}},
                            status=422)
        fee = request.data.get("adoption_fee") or "0"
        try:
            fee_dec = Decimal(str(fee))
        except (InvalidOperation, ValueError):
            return Response({"error": {"code": "bad_request", "message": "Invalid adoption_fee"}}, status=422)
        cap = fee_cap_for(request.user)
        if cap is not None and fee_dec > cap:
            return Response({"error": {"code": "fee_over_cap",
                                       "message": f"The adoption fee can't exceed ₱{cap}",
                                       "details": {"cap": cap}}}, status=422)
        with transaction.atomic():
            listing = AdoptionListing.objects.create(
                posted_by=request.user, source_report=case.report, species=case.report.species,
                name=request.data.get("name") or "",
                city=request.data.get("city") or case.report.city or "",
                adoption_fee=fee_dec, status=ListingStatus.PENDING)
            inquiry = AdoptionInquiry.objects.create(listing=listing, adopter_account=recipient,
                                                     status=InquiryStatus.ACTIVE)
            for key in AdoptionStageKey:
                stage = AdoptionStage.objects.create(inquiry=inquiry, stage_key=key)
                set_stage_state(stage, StageState.SKIPPED, request.user, note="direct placement")
            notify(recipient, "inquiry_received", title="You've been offered a pet",
                  body=f"{request.user.display_name} wants to place an animal with you.",
                  data={"listing_id": str(listing.pk), "inquiry_id": str(inquiry.pk)})
        return Response({"listing_id": str(listing.pk), "inquiry_id": str(inquiry.pk)}, status=201)


class ListingDetailView(APIView):
    """GET /listings/{id} (US-A3, Public) · PATCH /listings/{id} (US-A2, poster-only —
    ownership is the real gate here, not verification; see ListingsView's note)."""

    def get_permissions(self):
        return [AllowAny()] if self.request.method == "GET" else [IsAuthenticated()]

    def get(self, request, listing_id):
        listing = AdoptionListing.objects.filter(pk=listing_id).first()
        if listing is None:
            return Response({"error": {"code": "not_found", "message": "No such listing"}},
                            status=404)
        return Response({
            "listing_id": str(listing.pk), "pet": _pet_fields(listing),
            "description": listing.story or None, "adoption_fee": str(listing.adoption_fee),
            "requirements": listing.requirements or None, "city": listing.city,
            "status": listing.status,
            "photos": [p.url for p in listing.photos.all()],
            "poster": _poster_info(listing.posted_by),
        })

    def patch(self, request, listing_id):
        listing = AdoptionListing.objects.filter(pk=listing_id).first()
        if listing is None:
            return Response({"error": {"code": "not_found", "message": "No such listing"}},
                            status=404)
        if listing.posted_by_id != request.user.pk:
            return Response({"error": {"code": "not_your_listing",
                                       "message": "Only the poster can edit this listing"}},
                            status=403)
        s = ListingPatchSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        if "adoption_fee" in data:
            cap = fee_cap_for(request.user)
            if cap is not None and data["adoption_fee"] > cap:
                return Response({"error": {"code": "fee_over_cap",
                                           "message": f"The adoption fee can't exceed ₱{cap}",
                                           "details": {"cap": cap}}}, status=422)
        field_map = {"birthdate": "date_of_birth", "description": "story"}
        for key, value in data.items():
            setattr(listing, field_map.get(key, key), value)
        listing.save()
        return Response({
            "listing_id": str(listing.pk), "pet": _pet_fields(listing),
            "description": listing.story or None, "adoption_fee": str(listing.adoption_fee),
            "city": listing.city, "status": listing.status,
        })


class ListingInquiriesView(APIView):
    """POST /listings/{id}/inquiries — US-A4. Only a Verified Member with a verified
    phone may inquire; the phone check exists because the inquiry is the first
    contact-exchange moment ("verify-phone ships with its first trigger")."""
    permission_classes = [IsVerifiedMember]

    def post(self, request, listing_id):
        listing = AdoptionListing.objects.filter(pk=listing_id).first()
        if listing is None:
            return Response({"error": {"code": "not_found", "message": "No such listing"}},
                            status=404)
        if request.user.phone_verified_at is None:
            return Response({"error": {"code": "phone_unverified",
                                       "message": "Verify your phone before inquiring"}},
                            status=403)
        s = InquiryCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        if AdoptionInquiry.objects.filter(listing=listing, adopter_account=request.user).exists():
            return Response({"error": {"code": "already_inquired",
                                       "message": "You already inquired on this listing"}},
                            status=409)
        try:
            with transaction.atomic():
                inquiry = AdoptionInquiry.objects.create(
                    listing=listing, adopter_account=request.user,
                    message=s.validated_data.get("message", ""))
                stages = []
                for key in AdoptionStageKey:
                    stage = AdoptionStage.objects.create(inquiry=inquiry, stage_key=key)
                    stages.append(stage)
                # The 'inquiry' stage completes itself — submitting IS that stage
                # happening. Logged like any other transition (set_stage_state), not
                # silently defaulted, so the history is complete from the first row.
                inquiry_stage = next(st for st in stages if st.stage_key == AdoptionStageKey.INQUIRY)
                set_stage_state(inquiry_stage, StageState.DONE, request.user)
                notify(listing.posted_by, "inquiry_received",
                      title="Someone inquired about your listing",
                      body=f"{request.user.display_name} is interested in {listing.name}.",
                      data={"listing_id": str(listing.pk), "inquiry_id": str(inquiry.pk)})
        except IntegrityError:
            return Response({"error": {"code": "already_inquired",
                                       "message": "You already inquired on this listing"}},
                            status=409)
        return Response({"inquiry_id": str(inquiry.pk), "status": inquiry.status}, status=201)


class MyInquiriesView(APIView):
    """GET /me/inquiries — US-A4. The adopter's own inquiries, with each stage's state,
    so "both sides see the same state" is literal: this is the same data the poster's
    stage-advance writes into."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (AdoptionInquiry.objects.filter(adopter_account=request.user)
              .select_related("listing").order_by("-created_at"))
        page_items, next_page = _paginate(qs, request)
        results = []
        for inquiry in page_items:
            stages = {s.stage_key: s.state for s in inquiry.stages.all()}
            results.append({
                "inquiry_id": str(inquiry.pk),
                "listing": {"listing_id": str(inquiry.listing_id), "name": inquiry.listing.name,
                           "species": inquiry.listing.species},
                "status": inquiry.status,
                "stages": [{"stage_key": key, "state": stages.get(key, StageState.NOT_STARTED)}
                          for key in AdoptionStageKey],
            })
        return Response({"results": results, "next": next_page})


class MyPetsView(APIView):
    """GET /me/pets — US-H3. The owner's own pets, newest first, each with its primary
    photo (or earliest-uploaded if none is primary) for the mobile My-pets tab."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        def _repr(p):
            photos = sorted(p.photos.all(), key=lambda ph: ph.uploaded_at)
            primary = next((ph for ph in photos if ph.is_primary), None)
            ph = primary or (photos[0] if photos else None)
            return {"pet_id": str(p.pk), "name": p.name, "species": p.species,
                    "photo_url": (ph.url if ph else None)}
        pets = request.user.pets.order_by("-created_at").prefetch_related("photos")
        return Response({"results": [_repr(p) for p in pets]})


class InquiryStageView(APIView):
    """POST /inquiries/{id}/stages/{stage_key} — US-A4. Only the listing's poster may
    advance a stage."""
    permission_classes = [IsAuthenticated]

    def post(self, request, inquiry_id, stage_key):
        inquiry = AdoptionInquiry.objects.select_related("listing").filter(pk=inquiry_id).first()
        if inquiry is None:
            return Response({"error": {"code": "not_found", "message": "No such inquiry"}},
                            status=404)
        if inquiry.listing.posted_by_id != request.user.pk:
            return Response({"error": {"code": "not_your_listing",
                                       "message": "Only the poster can advance this inquiry"}},
                            status=403)
        stage = inquiry.stages.filter(stage_key=stage_key).first()
        if stage is None:
            return Response({"error": {"code": "not_found", "message": "No such stage"}},
                            status=404)
        s = StageUpdateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        set_stage_state(stage, s.validated_data["state"], request.user,
                        note=s.validated_data.get("note", ""))
        notify(inquiry.adopter_account, "stage_advanced",
              title="Your adoption inquiry moved forward",
              body=f"{stage_key.replace('_', ' ').title()} is now {s.validated_data['state'].replace('_', ' ')}.",
              data={"inquiry_id": str(inquiry.pk), "stage_key": stage_key})
        return Response({"stage_key": stage_key, "state": stage.state})


class PlacementDecisionView(APIView):
    """POST /inquiries/{id}/accept | /decline — US-H3. The recipient of a direct
    placement (all stages skipped) accepts or declines it. Accept is the first code
    path that ever writes a `Pet` row; decline frees the listing back up.

    The load + guard + mutation all happen inside one `transaction.atomic()` with a
    `select_for_update()` row lock on the inquiry, and the guard re-checks
    `inquiry.status == ACTIVE` (not just the stage snapshot, which stays
    {SKIPPED} forever). Without both of these: a concurrent double-submit could race
    past the guard before either write lands, and a *sequential* second accept/decline
    would sail through since 'all stages skipped' + 'you're the adopter' both remain
    true after the first decision — producing a duplicate Pet, an orphaned Pet
    (decline-after-accept re-frees a listing whose Pet already exists), or a reversed
    adoption (decline-then-accept)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, inquiry_id, action):
        with transaction.atomic():
            inq = (AdoptionInquiry.objects.select_for_update()
                   .select_related("listing").filter(pk=inquiry_id).first())
            if inq is None:
                return Response({"error": {"code": "not_found", "message": "No such inquiry"}},
                                status=404)
            if inq.adopter_account_id != request.user.pk:
                return Response({"error": {"code": "not_your_placement",
                                           "message": "Only the recipient can decide this"}},
                                status=403)
            states = set(AdoptionStage.objects.filter(inquiry=inq).values_list("state", flat=True))
            if states != {StageState.SKIPPED}:
                return Response({"error": {"code": "not_a_placement",
                                           "message": "This isn't a direct placement"}},
                                status=409)
            if inq.status != InquiryStatus.ACTIVE:
                return Response({"error": {"code": "already_decided",
                                           "message": "This placement was already decided"}},
                                status=409)

            now = timezone.now()
            if action == "accept":
                inq.status = InquiryStatus.ADOPTED
                inq.decided_at = now
                inq.save(update_fields=["status", "decided_at"])
                pet = Pet.objects.create(owner_account=request.user,
                                         name=inq.listing.name or "Pet",
                                         species=inq.listing.species)
                for ph in inq.listing.photos.all():
                    PetPhoto.objects.create(pet=pet, url=ph.url, is_primary=ph.is_primary)
                inq.listing.status = ListingStatus.ADOPTED
                inq.listing.adopted_pet = pet
                inq.listing.adopted_by_account = request.user
                inq.listing.save(update_fields=["status", "adopted_pet", "adopted_by_account"])
                # US-B1 · rehoming a pet can earn a badge for the lister (idempotent +
                # reconciled nightly; deferred import avoids a cycle).
                from community.badges import award_badges_for
                award_badges_for(inq.listing.posted_by)
                from common.analytics import emit
                emit("inquiry_decided", outcome="accepted")
                emit("adoption_completed")
                return Response({"pet_id": str(pet.pk)}, status=200)
            # decline
            inq.status = InquiryStatus.DECLINED
            inq.decided_at = now
            inq.save(update_fields=["status", "decided_at"])
            inq.listing.status = ListingStatus.AVAILABLE
            inq.listing.save(update_fields=["status"])
            from common.analytics import emit
            emit("inquiry_decided", outcome="declined")
            return Response(status=200)
