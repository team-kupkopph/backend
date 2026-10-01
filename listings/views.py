from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Account, AccountStatus, Address
from common.cities import city_variants
from listings.fees import fee_cap_for
from listings.models import (
    AdoptionInquiry,
    AdoptionListing,
    AdoptionListingPhoto,
    AdoptionStage,
    AdoptionStageKey,
    InquiryStatus,
    ListingPreference,
    ListingStatus,
    Pet,
    PetPhoto,
    PreferenceKind,
    StageState,
)
from listings.permissions import IsVerifiedMember
from listings.serializers import (
    InquiryCreateSerializer,
    ListingCreateSerializer,
    ListingPatchSerializer,
    StageUpdateSerializer,
)
from listings.stages import set_stage_state
from listings.visibility import account_is_verified_rescuer, public_poster_q
from notifications.service import notify
from sagip import notices
from sagip.models import RescueCase, StrayStatus
from sagip.status import resolve_report
from shelter.models import ShelterProfile

PAGE_SIZE = 20


# S20 · a listing in any of these states is the animal's live handoff; a WITHDRAWN one
# (a declined placement, or a listing its poster took down) no longer is.
# D12 · a rescue's draft is its handoff in progress.
LIVE_HANDOFF_STATUSES = (ListingStatus.DRAFT, ListingStatus.AVAILABLE,
                         ListingStatus.PENDING, ListingStatus.ADOPTED)


def _load_safe_own_case(case_id, user):
    """H1's safe/own-case gate, shared by `CaseListView` and `CasePlaceView`: the case
    must exist, be claimed by the requesting user, its report must be SAFE, and it must
    not already have a live handoff. Returns (case, None) on success or (None, Response)
    with the appropriate error status.

    ⚠️ Call inside `transaction.atomic()`: the case row is locked so two taps on "Place"
    (or a List racing a Place) serialize here and the second one sees the first's listing.
    Without the lock and the handoff check, one case could be listed AND placed, or placed
    twice (S20, dev/sagip-build-review.md)."""
    case = (RescueCase.objects.select_for_update().select_related("report")
            .filter(pk=case_id).first())
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
    if AdoptionListing.objects.filter(source_report=case.report,
                                      status__in=LIVE_HANDOFF_STATUSES).exists():
        return None, Response({"error": {"code": "already_handed_off",
                                         "message": "This animal is already listed or offered "
                                                    "to someone"}},
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


def _card(listing, poster=None):
    photo = listing.photos.filter(is_primary=True).first() or listing.photos.first()
    card = {"listing_id": str(listing.listing_id), "pet": _pet_fields(listing),
            "city": listing.city, "status": listing.status,
            "adoption_fee": str(listing.adoption_fee),
            "photo_url": photo.url if photo else None}
    # The list card carries the poster too, so the Adopt deck can show who is offering the
    # animal without a detail fetch per card. Passed in from `_poster_infos` (bulk), never
    # looked up here — a page of 20 cards must not cost 40 queries.
    if poster is not None:
        card["poster"] = poster
    return card


def _poster_infos(accounts):
    """`_poster_info` for a page of listings in two queries instead of two per card."""
    ids = {a.pk for a in accounts}
    profiles = {p.account_id: p for p in ShelterProfile.objects.filter(account_id__in=ids)}
    addrs = {a.account_id: a for a in Address.objects.filter(account_id__in=ids, is_primary=True)}
    out = {}
    for account in accounts:
        profile, addr = profiles.get(account.pk), addrs.get(account.pk)
        out[account.pk] = {"account_id": str(account.pk),
                           "name": profile.org_name if profile else account.display_name,
                           "is_shelter": profile is not None, "city": addr.city if addr else None}
    return out


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
            status = request.query_params.get("status") or "available"
            if status not in ListingStatus.values:
                return Response({"error": {"code": "bad_status",
                                           "message": "Unknown status"}}, status=422)
            qs = (AdoptionListing.objects.filter(status=status, posted_by=request.user)
                  .select_related("posted_by").order_by("-created_at"))
            page_items, next_page = _paginate(qs, request)
            posters = _poster_infos({item.posted_by for item in page_items})
            return Response({"results": [_card(item, posters[item.posted_by_id]) for item in page_items],
                             "next": next_page})
        # US-N1 · a deleted account's listings leave every public surface at once.
        qs = (AdoptionListing.objects.filter(status="available")
              .exclude(posted_by__status=AccountStatus.DELETED)
              .filter(public_poster_q()).select_related("posted_by").distinct().order_by("-created_at"))
        city = request.query_params.get("city")
        if city:
            # ⚠️ Not `city=city`. The listing form is free text while the mobile picker has a
            # fixed vocabulary, so an exact match dropped every "Marikina" row for a viewer
            # whose city reads "Marikina City" — see common/cities.py.
            variants = city_variants(city)
            if variants:
                match = Q()
                for variant in variants:
                    match |= Q(city__iexact=variant)
                qs = qs.filter(match)
        species = request.query_params.get("species")
        if species:
            qs = qs.filter(species=species)
        page_items, next_page = _paginate(qs, request)
        posters = _poster_infos({item.posted_by for item in page_items})
        return Response({"results": [_card(item, posters[item.posted_by_id]) for item in page_items],
                         "next": next_page})

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
    """US-H1 · list an adoption from a SAFE rescue case. The listing is a private DRAFT
    carrying the report's photos (C15/D12). It carries source_report so provenance
    survives; the animal's species is inherited from the report. Fee capped
    by the existing fee_cap_for — no second rule."""
    permission_classes = [IsAuthenticated]

    def post(self, request, case_id):
        with transaction.atomic():
            case, error = _load_safe_own_case(case_id, request.user)
            if error:
                return error
            fee = request.data.get("adoption_fee") or "0"
            try:
                fee_dec = Decimal(str(fee))
            except (InvalidOperation, ValueError):
                return Response({"error": {"code": "bad_request", "message": "Invalid adoption_fee"}},
                                status=422)
            cap = fee_cap_for(request.user)
            if cap is not None and fee_dec > cap:
                return Response({"error": {"code": "fee_over_cap",
                                           "message": f"The adoption fee can't exceed ₱{cap}",
                                           "details": {"cap": cap}}}, status=422)
            listing = AdoptionListing.objects.create(
                posted_by=request.user, source_report=case.report, species=case.report.species,
                name=request.data.get("name") or "",
                city=request.data.get("city") or case.report.city or "",
                adoption_fee=fee_dec, status=ListingStatus.DRAFT)
            # D12 · the rescuer finishes the story, fee and details, then publishes
            # (POST /listings/{id}/publish) — the path a shelter's D7 draft already takes.
            urls = [p.url for p in case.report.photos.order_by("uploaded_at")]
            for i, url in enumerate(urls):
                AdoptionListingPhoto.objects.create(listing=listing, url=url, is_primary=(i == 0))
        return Response({"listing_id": str(listing.pk), "draft": True}, status=201)


class CasePlaceView(APIView):
    """US-H2 · direct placement to a verified recipient — the rescuer hands a SAFE
    case's animal straight to a known Verified Member or shelter, bypassing the public
    inquiry flow. Reuses H1's safe/own-case gate. Every stage is created then immediately
    moved to SKIPPED (the placement bypass — US-H3 keys off "all stages skipped")."""
    permission_classes = [IsAuthenticated]

    def post(self, request, case_id):
        # One transaction from the gate to the notify: the gate locks the case row, so a
        # second Place (or a List) waits here and then sees this listing (S20).
        with transaction.atomic():
            case, error = _load_safe_own_case(case_id, request.user)
            if error:
                return error
            recipient = Account.objects.filter(email=request.data.get("recipient_email")).first()
            if recipient is None:
                return Response({"error": {"code": "recipient_not_found", "message": "No such account"}},
                                status=404)
            if recipient.pk == request.user.pk:
                return Response({"error": {"code": "recipient_is_you",
                                           "message": "Choose someone other than yourself"}},
                                status=422)
            if not account_is_verified_rescuer(recipient):
                return Response({"error": {"code": "recipient_not_verified",
                                           "message": "The recipient must be a verified member or shelter"}},
                                status=422)
            fee = request.data.get("adoption_fee") or "0"
            try:
                fee_dec = Decimal(str(fee))
            except (InvalidOperation, ValueError):
                return Response({"error": {"code": "bad_request", "message": "Invalid adoption_fee"}},
                                status=422)
            cap = fee_cap_for(request.user)
            if cap is not None and fee_dec > cap:
                return Response({"error": {"code": "fee_over_cap",
                                           "message": f"The adoption fee can't exceed ₱{cap}",
                                           "details": {"cap": cap}}}, status=422)
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


def _withdraw_placement(inquiry, now):
    """C14 · end an unanswered direct placement: the offer is withdrawn, the listing goes back to
    private (WITHDRAWN frees the case for its next handoff, S20), and the recipient is told."""
    inquiry.status = InquiryStatus.WITHDRAWN
    inquiry.decided_at = now
    inquiry.save(update_fields=["status", "decided_at"])
    inquiry.listing.status = ListingStatus.WITHDRAWN
    inquiry.listing.save(update_fields=["status"])
    notices.placement_withdrawn(inquiry.listing, inquiry)


class CaseHandoffCancelView(APIView):
    """C14 / D11 · POST /cases/{id}/handoff/cancel — the rescuer takes back a handoff that hasn't
    happened yet: a draft, an unanswered placement, or a public listing nobody has inquired on.

    ⚠️ Lock order is case -> listing -> inquiries; PlacementDecisionView locks the inquiry
    first. A cancel racing an accept can therefore deadlock; Postgres aborts one side with a
    deadlock error and rolls it back whole, so state stays consistent (see the task report)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, case_id):
        with transaction.atomic():
            case = (RescueCase.objects.select_for_update().select_related("report")
                    .filter(pk=case_id).first())
            if case is None:
                return Response({"error": {"code": "not_found", "message": "No such case"}}, status=404)
            if case.claimed_by_account_id != request.user.pk:
                return Response({"error": {"code": "not_your_case",
                                           "message": "Only the claiming rescuer can change this"}},
                                status=403)
            if case.report.status != StrayStatus.SAFE:
                return Response({"error": {"code": "case_not_safe",
                                           "message": "There is no handoff to take back"}}, status=409)
            listing = (AdoptionListing.objects.select_for_update()
                       .filter(source_report=case.report, status__in=LIVE_HANDOFF_STATUSES).first())
            if listing is None:
                return Response({"error": {"code": "no_handoff",
                                           "message": "This animal isn't listed or offered to anyone"}},
                                status=404)
            if listing.status == ListingStatus.ADOPTED:
                return Response({"error": {"code": "already_adopted",
                                           "message": "This animal already has a home"}}, status=409)
            active = list(AdoptionInquiry.objects.select_for_update()
                          .filter(listing=listing, status=InquiryStatus.ACTIVE))
            now = timezone.now()
            if listing.status == ListingStatus.PENDING and len(active) == 1:
                _withdraw_placement(active[0], now)
            elif active:
                return Response({"error": {"code": "has_active_inquiries",
                                           "message": "People have asked about this animal — reply "
                                                      "to them before taking the listing down"}},
                                status=409)
            else:
                listing.status = ListingStatus.WITHDRAWN
                listing.save(update_fields=["status"])
        return Response({"status": "withdrawn"})


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
        # D7 · a draft is private to its poster — to anyone else it doesn't exist yet.
        if listing.status == ListingStatus.DRAFT and listing.posted_by_id != getattr(request.user, "pk", None):
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
        if listing.status == ListingStatus.DRAFT:          # D7 · not public yet
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


def _stage_json(key, stage):
    if stage is None or stage.state == StageState.NOT_STARTED:
        return {"stage_key": key, "state": StageState.NOT_STARTED, "updated_at": None, "note": None}
    return {"stage_key": key, "state": stage.state,
            "updated_at": stage.updated_at.isoformat(), "note": stage.note or None}


def _my_inquiry_row(inquiry):
    """One adopter-facing inquiry: the /me/inquiries row, and the GET /inquiries/{id} body."""
    stages = {s.stage_key: s for s in inquiry.stages.all()}
    return {
        "inquiry_id": str(inquiry.pk),
        "listing": {"listing_id": str(inquiry.listing_id), "name": inquiry.listing.name,
                   "species": inquiry.listing.species},
        "status": inquiry.status,
        # All six rows exist from the inquiry's first second, so a row's `updated_at`
        # is only a date the ladder should show once the stage has MOVED — for a
        # `not_started` stage it is the creation time, and is sent as null. `note`
        # is null when empty for the same reason: absent, not "".
        "stages": [_stage_json(key, stages.get(key)) for key in AdoptionStageKey],
    }


class MyInquiriesView(APIView):
    """GET /me/inquiries — US-A4. The adopter's own inquiries, with each stage's state,
    so "both sides see the same state" is literal: this is the same data the poster's
    stage-advance writes into."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (AdoptionInquiry.objects.filter(adopter_account=request.user)
              .select_related("listing").order_by("-created_at"))
        page_items, next_page = _paginate(qs, request)
        results = [_my_inquiry_row(inquiry) for inquiry in page_items]
        return Response({"results": results, "next": next_page})


class InquiryDetailView(APIView):
    """C25 · one of the caller's own inquiries, by id — the Place request screen used to scan page 1
    of /me/inquiries and called an older placement "not found"."""
    permission_classes = [IsAuthenticated]

    def get(self, request, inquiry_id):
        inquiry = (AdoptionInquiry.objects.select_related("listing")
                   .filter(pk=inquiry_id, adopter_account=request.user).first())
        if inquiry is None:
            return Response({"error": {"code": "not_found", "message": "No such inquiry"}}, status=404)
        return Response(_my_inquiry_row(inquiry))


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


def _shelter_draft_from(placed, shelter):
    """D7 · the shelter's own listing for an animal it just took in from a rescue: a private
    DRAFT posted by the shelter, carrying what is known — species, name, the rescue it came from
    (provenance), the shelter's own city (where the animal now is), and the photos (the
    placement's, else the stray report's; the first is primary). Story, fee and requirements
    are the shelter's to write before it publishes."""
    primary = shelter.addresses.filter(is_primary=True).first()
    draft = AdoptionListing.objects.create(
        posted_by=shelter, source_report=placed.source_report, species=placed.species,
        name=placed.name or "", city=(primary.city if primary else placed.city) or "",
        status=ListingStatus.DRAFT)
    urls = [p.url for p in placed.photos.order_by("-is_primary")]
    if not urls and placed.source_report_id:
        urls = [p.url for p in placed.source_report.photos.order_by("uploaded_at")]
    for i, url in enumerate(urls):
        AdoptionListingPhoto.objects.create(listing=draft, url=url, is_primary=(i == 0))
    return draft


class ListingPublishView(APIView):
    """D7 · POST /listings/{id}/publish — the poster puts a DRAFT on the Adopt feed. Poster only,
    and only from draft; the public feed's own gates (a verified poster) still apply after."""
    permission_classes = [IsAuthenticated]

    def post(self, request, listing_id):
        with transaction.atomic():
            listing = AdoptionListing.objects.select_for_update().filter(pk=listing_id).first()
            if listing is None:
                return Response({"error": {"code": "not_found", "message": "No such listing"}},
                                status=404)
            if listing.posted_by_id != request.user.pk:
                return Response({"error": {"code": "not_your_listing",
                                           "message": "Only the poster can publish this listing"}},
                                status=403)
            if listing.status != ListingStatus.DRAFT:
                return Response({"error": {"code": "not_draft",
                                           "message": "Only a draft can be published"}}, status=409)
            listing.status = ListingStatus.AVAILABLE
            listing.save(update_fields=["status", "updated_at"])
        return Response({"listing_id": str(listing.pk), "status": listing.status})


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
                # D7 · a SHELTER taking in a rescue gets the animal as a draft in its own
                # Animals tab, to describe and publish; a person adopts it as a Pet.
                if request.user.account_type == "shelter":
                    pet, draft = None, _shelter_draft_from(inq.listing, request.user)
                else:
                    draft = None
                    pet = Pet.objects.create(owner_account=request.user,
                                             name=inq.listing.name or "Pet",
                                             species=inq.listing.species)
                    for ph in inq.listing.photos.all():
                        PetPhoto.objects.create(pet=pet, url=ph.url, is_primary=ph.is_primary)
                inq.listing.status = ListingStatus.ADOPTED
                inq.listing.adopted_pet = pet
                inq.listing.adopted_by_account = request.user
                inq.listing.save(update_fields=["status", "adopted_pet", "adopted_by_account"])
                # S2 · a placed rescue is a finished rescue: resolve the report and close the
                # case in the same transaction, or the rescuer's Home keeps asking them to
                # find a home for an animal that has one. The recipient is the actor.
                if inq.listing.source_report_id is not None:
                    report = inq.listing.source_report
                    if resolve_report(report, request.user, note="direct placement accepted"):
                        case = report.cases.filter(expired_at__isnull=True).first()
                        if case is not None:
                            notices.case_progress(report, case, StrayStatus.RESOLVED)   # S10
                notices.placement_decided(inq.listing, inq, "accepted")   # S18
                # US-B1 · rehoming a pet can earn a badge for the lister (idempotent +
                # reconciled nightly; deferred import avoids a cycle).
                from community.badges import award_badges_for
                award_badges_for(inq.listing.posted_by)
                from common.analytics import emit
                emit("inquiry_decided", outcome="accepted")
                emit("adoption_completed")
                if draft is not None:
                    return Response({"listing_id": str(draft.pk), "draft": True}, status=200)
                return Response({"pet_id": str(pet.pk)}, status=200)
            # decline
            inq.status = InquiryStatus.DECLINED
            inq.decided_at = now
            inq.save(update_fields=["status", "decided_at"])
            # ⚠️ WITHDRAWN, not AVAILABLE. A placement listing was never public — the rescuer
            # chose one person, not the adoption feed — so a decline must not publish it. It
            # also frees the case for its next handoff (S20's guard ignores withdrawn rows).
            inq.listing.status = ListingStatus.WITHDRAWN
            inq.listing.save(update_fields=["status"])
            notices.placement_decided(inq.listing, inq, "declined")   # S18
            from common.analytics import emit
            emit("inquiry_decided", outcome="declined")
            return Response(status=200)


class ShortlistView(APIView):
    """GET /me/shortlist — the adopter's saved and hidden listing ids, newest first. Ids only:
    the deck intersects them with the feed it already fetched, and a listing that has since
    gone is simply not in the feed. (The rows themselves go with the listing — CASCADE.)"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = (ListingPreference.objects.filter(account=request.user)
                .order_by("-updated_at").values_list("listing_id", "kind"))
        out = {"saved": [], "hidden": []}
        for listing_id, kind in rows:
            out[kind].append(str(listing_id))
        return Response(out)


class ShortlistItemView(APIView):
    """PUT /me/shortlist/{listing_id} {"kind": "saved"|"hidden"} · DELETE /me/shortlist/{listing_id}.

    Both idempotent, on purpose: the client writes optimistically and retries after a dead
    spot, so a repeated PUT must land on the same row and a repeated DELETE must be a 204,
    not a 404. PUT with the other kind replaces the row — a save after a hide is one row,
    which is the deck's own rule. The only gate is that the listing exists: a preference is
    not a read of the listing, and refusing a hide on a listing the caller cannot see would
    say that it exists."""
    permission_classes = [IsAuthenticated]

    def put(self, request, listing_id):
        kind = request.data.get("kind")
        if kind not in PreferenceKind.values:
            return Response({"error": {"code": "invalid", "message": "kind must be saved or hidden"}},
                            status=422)
        if not AdoptionListing.objects.filter(pk=listing_id).exists():
            return Response({"error": {"code": "not_found", "message": "No such listing"}}, status=404)
        row, _ = ListingPreference.objects.update_or_create(
            account=request.user, listing_id=listing_id, defaults={"kind": kind})
        return Response({"listing_id": str(listing_id), "kind": row.kind,
                         "updated_at": row.updated_at.isoformat()})

    def delete(self, request, listing_id):
        ListingPreference.objects.filter(account=request.user, listing_id=listing_id).delete()
        return Response(status=204)
