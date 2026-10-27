from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import AccountStatus, Address
from common.throttles import DonationQrUploadThrottle
from listings.models import AdoptionInquiry, InquiryKind, ListingStatus
from shelter.models import DonationQr, QrStatus, ShelterProfile
from shelter.permissions import IsShelter
from shelter.qr_rules import discard_image, own_repr
from shelter.serializers import (
    DonationQrUploadSerializer,
    ShelterProfileCreateSerializer,
    ShelterProfilePatchSerializer,
)
from verifications.models import VerificationRequest
from volunteer.models import VolunteerShift, VolunteerSignup

REQUESTS_PAGE_SIZE = 20


class ShelterProfileView(APIView):
    permission_classes = [IsShelter]

    def post(self, request):
        # US-B2 requires a verified email before an org profile can be created.
        if request.user.email_verified_at is None:
            return Response({"error": {"code": "email_unverified",
                                       "message": "Verify your email first"}}, status=403)
        if ShelterProfile.objects.filter(account=request.user).exists():
            return Response({"error": {"code": "profile_exists",
                                       "message": "A shelter profile already exists"}}, status=409)
        s = ShelterProfileCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        addr = data["address"]
        with transaction.atomic():
            profile = ShelterProfile.objects.create(
                account=request.user, org_name=data["org_name"], org_type=data["org_type"],
                tier=data["tier"], registration_type=data.get("registration_type") or None,
                registration_number=data.get("registration_number", ""),
                logo_url=data.get("logo_file_url", ""))
            # The org address is city-level like a person's, but may carry line1/barangay/
            # province the shelter chose to share. No geom is stored unless later provided.
            Address.objects.update_or_create(
                account=request.user, is_primary=True,
                defaults={"line1": addr.get("line1", ""), "barangay": addr.get("barangay", ""),
                          "city": addr["city"], "province": addr.get("province", ""), "geom": None})
        return Response({"shelter_profile_id": str(profile.shelter_profile_id)}, status=201)

    def get(self, request):
        """T8 · the shelter's own full profile. Before this the Donation instructions screen read
        its own text back through the PUBLIC needs route, and nothing could show the rest."""
        profile = ShelterProfile.objects.filter(account=request.user).first()
        if profile is None:
            return Response({"error": {"code": "no_profile",
                                       "message": "Create the shelter profile first"}}, status=404)
        return Response(_profile_repr(profile))

    def patch(self, request):
        profile = ShelterProfile.objects.filter(account=request.user).first()
        if profile is None:
            return Response({"error": {"code": "no_profile",
                                       "message": "Create the shelter profile first"}}, status=409)
        s = ShelterProfilePatchSerializer(data=request.data, partial=True,
                                          context={"request": request})
        s.is_valid(raise_exception=True)
        data = dict(s.validated_data)
        # AS7 · the org name is what the reviewers verified. Once ANY shelter_org verification is
        # approved it can no longer change here — otherwise a verified shelter could become a
        # different organisation under the same badge. Re-sending the current name is not a rename
        # (the edit screen submits the whole form).
        if "org_name" in data and data["org_name"] != profile.org_name:
            if request.user.verifications.filter(type="shelter_org", status="approved").exists():
                return Response({"error": {
                    "code": "org_name_locked", "field": "org_name",
                    "message": "Your organization's name is what we verified. "
                               "Contact us to change it."}}, status=409)
        if "logo_file_url" in data:
            profile.logo_url = data.pop("logo_file_url")
        for key, value in data.items():
            setattr(profile, key, value)
        profile.save()
        return Response(_profile_repr(profile))


class ShelterRequestsView(APIView):
    """B-be2 · GET /shelter/requests — one merged inbox across the shelter's three
    inbound-request surfaces: adoption inquiries on its own listings, volunteer signups
    on its own shifts, and placements addressed to it (a case-worker `.../place`d
    inquiry where the shelter is the *adopter* and every stage is SKIPPED — the
    placement bypass; see listings/tests/test_inquiries.py). Merged and sorted in
    Python rather than in SQL: the three sources are different models with no shared
    table to `UNION`, and shelf-inbox volumes make an in-memory merge cheap enough."""
    permission_classes = [IsShelter]

    def get(self, request):
        kind = request.query_params.get("kind")
        open_only = request.query_params.get("status") == "open"

        items = []
        if kind in (None, "adoption"):
            items += self._adoption_items(request.user)
        if kind in (None, "volunteer"):
            items += self._volunteer_items(request.user)
        if kind in (None, "placement"):
            items += self._placement_items(request.user)

        if open_only:
            items = [i for i in items if i["status"] in ("active", "requested")]
        items.sort(key=lambda i: i["created_at"], reverse=True)

        page_items, next_page = _paginate_items(items, request)
        for item in page_items:
            item["created_at"] = item["created_at"].isoformat()
        return Response({"results": page_items, "next": next_page})

    @staticmethod
    def _adoption_items(shelter):
        # `listing__posted_by=shelter` covers everything the shelter posted; placements
        # (`kind=placement`) are excluded here and surfaced separately by
        # `_placement_items` instead, keyed off who the *adopter* is.
        qs = (AdoptionInquiry.objects.filter(listing__posted_by=shelter)
              .select_related("listing", "adopter_account").order_by("-created_at"))
        items = []
        for inq in qs:
            if _is_placement(inq):
                continue
            items.append({
                "kind": "adoption", "id": str(inq.pk), "title": inq.listing.name,
                "subtitle": inq.adopter_account.display_name, "status": inq.status,
                "created_at": inq.created_at,
                "target": {"route": "inquiry", "id": str(inq.pk)},
            })
        return items

    @staticmethod
    def _volunteer_items(shelter):
        qs = (VolunteerSignup.objects.filter(shift__shelter_account=shelter)
              .select_related("shift", "volunteer_account").order_by("-created_at"))
        return [{
            "kind": "volunteer", "id": str(su.pk),
            "title": su.volunteer_account.display_name,
            "subtitle": (f"{timezone.localtime(su.shift.starts_at):%a, %b %-d} · "
                         f"{su.shift.title or su.shift.get_type_display()}"),
            "status": su.status, "created_at": su.created_at,
            "target": {"route": "shelterVolunteerRequests", "id": str(su.shift_id)},
        } for su in qs]

    @staticmethod
    def _placement_items(shelter):
        qs = (AdoptionInquiry.objects.filter(adopter_account=shelter)
              .select_related("listing", "listing__posted_by").order_by("-created_at"))
        items = []
        for inq in qs:
            if not _is_placement(inq):
                continue
            items.append({
                "kind": "placement", "id": str(inq.pk), "title": inq.listing.name,
                "subtitle": f"Placed by {inq.listing.posted_by.display_name}",
                "status": inq.status, "created_at": inq.created_at,
                "target": {"route": "inquiry", "id": str(inq.pk)},
            })
        return items


def _is_placement(inquiry):
    """A placement is an `AdoptionInquiry` created via the case-worker `.../place` path
    (decision 9's placement bypass). AD22 · said by its `kind` column: the old test (every
    stage SKIPPED) broke once AQ5 started public ladders with skipped stages."""
    return inquiry.kind == InquiryKind.PLACEMENT


def _paginate_items(items, request):
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    start = (page - 1) * REQUESTS_PAGE_SIZE
    page_items = items[start:start + REQUESTS_PAGE_SIZE]
    has_next = len(items) > start + REQUESTS_PAGE_SIZE
    return page_items, (page + 1 if has_next else None)


class ShelterDashboardView(APIView):
    permission_classes = [IsShelter]

    def get(self, request):
        # Everything here is derived, no stored gate/flag (§3.5). `submitted` = a
        # shelter_org verification_request exists; publish/donations open only when it
        # is approved (approval itself is Sprint 2 — Sprint 1 only ever shows pending).
        vr = (request.user.verifications.filter(type="shelter_org")
              .order_by("-submitted_at").first())
        submitted = vr is not None
        # `approved` = ANY approved shelter_org request (US-X4), matching public_poster_q() so the
        # dashboard and listing visibility never disagree. `vr` (latest) still drives the status/docs
        # shown, so an in-flight tier-2 upgrade reads as pending WITHOUT revoking the tier-1 gates.
        approved = request.user.verifications.filter(type="shelter_org", status="approved").exists()
        docs = [{"doc_type": d.doc_type, "status": d.status}
                for d in vr.documents.all()] if vr else []
        draft_listings = request.user.listings.filter(status=ListingStatus.DRAFT).count()   # AD15
        # US-X3 · donations are a TWO-key gate: org approved AND a reviewer-verified QR on file.
        # Still fully derived (§3.5) — no stored donations flag; the QR's `verified` is the check.
        donations_enabled = approved and request.user.donation_qrs.filter(status=QrStatus.LIVE).exists()
        # K27/G12 · the inbox row and dashboard both need a volunteer-facing summary: how
        # many signups are awaiting a decision, how many past shifts still need attendance
        # marked, and what's coming up next — all derived from VolunteerShift/Signup, no
        # stored counters (same §3.5 discipline as the rest of this view).
        now = timezone.now()
        mine = VolunteerShift.objects.filter(shelter_account=request.user)
        nxt = (mine.filter(starts_at__gt=now, status__in=["open", "full"])
               .order_by("starts_at").first())
        volunteer = {
            "pending_requests": VolunteerSignup.objects.filter(
                shift__shelter_account=request.user, status="requested",
                shift__ends_at__gt=now).count(),
            "attendance_due": VolunteerSignup.objects.filter(
                shift__shelter_account=request.user, status="approved",
                shift__ends_at__lte=now).count(),
            "next_shift": ({"shift_id": str(nxt.pk), "title": nxt.title or nxt.get_type_display(),
                            "starts_at": nxt.starts_at.isoformat()} if nxt else None),
        }
        return Response({
            "rescue": _rescue_summary(request.user),
            "verification": {"submitted": submitted,
                             "status": vr.status if vr else None, "docs": docs},
            "counts": {"draft_listings": draft_listings,
                       "live_listings": request.user.listings.filter(
                           status=ListingStatus.AVAILABLE, hidden_at__isnull=True).count(),
                       "adopted": request.user.listings.filter(status=ListingStatus.ADOPTED).count(),
                       # T8 · both were hard-coded 0 on the shelter's own profile (X13). Derived,
                       # like everything here (§3.5): people who took part, and pledges that arrived.
                       "volunteers": _volunteers_count(request.user),
                       "donations": _donations_count(request.user)},
            "gates": {"can_publish": approved, "donations_enabled": donations_enabled},
            "volunteer": volunteer,
            "wishlist": _wishlist_summary(request.user),
        })


def _volunteers_count(shelter):
    """Distinct people who took part in this shelter's shifts (approved or completed) — a person
    who came to five shifts is one volunteer. Requested, declined, cancelled and no-show don't count."""
    return (VolunteerSignup.objects.filter(shift__shelter_account=shelter,
                                           status__in=["approved", "completed"])
            .values("volunteer_account_id").distinct().count())


def _donations_count(shelter):
    """Pledges that actually ARRIVED (the shelter confirmed receipt), not pledges still waiting."""
    from community.models import NeedPledge, PledgeStatus
    return NeedPledge.objects.filter(need__shelter_account=shelter,
                                     status=PledgeStatus.DELIVERED).count()


def _wishlist_summary(shelter):
    """US-W3 · the Home card's wishlist numbers, derived (§3.5). A leftover `pledged` pledge on a
    `fulfilled` need still needs confirming (R1); a `closed` need has none."""
    from community.models import NeedPledge, NeedStatus, PledgeStatus, ShelterNeed
    return {
        "open_needs": ShelterNeed.objects.filter(shelter_account=shelter,
                                                 status=NeedStatus.OPEN).count(),
        "pledges_awaiting_confirm": NeedPledge.objects.filter(
            need__shelter_account=shelter, status=PledgeStatus.PLEDGED,
            need__status__in=[NeedStatus.OPEN, NeedStatus.FULFILLED]).count(),
    }


def _rescue_summary(shelter):
    """S16 · what Sagip needs from this shelter, for its Home's Rescue card. `needs_help` is the
    rescue map's own query (sagip.queries.reports_near_city) narrowed to unclaimed, non-lost
    reports — the card's number is the list it opens. It is None, never 0, when the shelter has
    no city the map can search: "0 near you" there would be a false statement."""
    from sagip.models import ReportType, RescueCase, StrayStatus
    from sagip.queries import reports_near_city

    primary = shelter.addresses.filter(is_primary=True).first()
    city = primary.city if primary else None
    near = reports_near_city(city)
    return {
        "city": city,
        "city_supported": near is not None,
        "needs_help": (near.filter(status=StrayStatus.REPORTED)
                       .exclude(report_type=ReportType.LOST).count() if near is not None else None),
        "open_cases": (RescueCase.objects
                       .filter(claimed_by_account=shelter, expired_at__isnull=True)
                       .exclude(report__status=StrayStatus.RESOLVED).count()),
    }


class DonationQrView(APIView):
    """US-Q1 / DW2 · the shelter's own QRs. A first upload or a replacement creates a `pending` row; it NEVER touches the live row, so the reviewed
    code stays public until the reviewer verifies the replacement. Uploading is not gated on the org's own approval (decision 2's draft-first
    pattern); the public gate (US-Q2) is the separate, always-both-keys check. Replacing a LIVE QR needs the account password (DW2 Q3)."""
    permission_classes = [IsShelter]

    def get_throttles(self):
        # AT18 · only the write is throttled; reading your own QRs is free.
        return [DonationQrUploadThrottle()] if self.request.method == "POST" else []

    def get(self, request):
        """AT3 · what the shelter itself can see: per provider the live row, the pending replacement, and the latest refusal (with its reason)."""
        rows = list(DonationQr.objects.filter(account=request.user).order_by("created_at"))
        out, refused_seen = [], set()
        for r in reversed(rows):                       # newest first so we keep only the LATEST refusal per provider
            if r.status in (QrStatus.LIVE, QrStatus.PENDING):
                out.append(r)
            elif r.status == QrStatus.REFUSED and r.provider not in refused_seen:
                refused_seen.add(r.provider)
                out.append(r)
        out.sort(key=lambda r: (r.provider, r.status != QrStatus.LIVE))
        return Response({"donation_qrs": [own_repr(r) for r in out]})

    def post(self, request):
        s = DonationQrUploadSerializer(data=request.data, context={"request": request})
        s.is_valid(raise_exception=True)
        d = s.validated_data
        with transaction.atomic():
            mine = DonationQr.objects.select_for_update().filter(account=request.user, provider=d["provider"])
            live = mine.filter(status=QrStatus.LIVE).first()
            # Owner decision Q1: a refusal is final for THAT image; a different one is fine.
            if mine.filter(status=QrStatus.REFUSED, qr_image_url=d["file_url"]).exists():
                return Response({"error": {"code": "refused_image", "field": "file_url",
                                           "message": "That image was already refused. Upload a different one."}}, status=400)
            # Owner decision Q3: only a REPLACEMENT needs re-auth (a first upload has no live code to swap). A social-only account has no password.
            if live is not None and request.user.password_hash:
                if not request.user.check_password(d.get("current_password") or ""):
                    return Response({"error": {"code": "reauth_required",
                                               "message": "Enter your password to replace a live donation QR."}}, status=403)
            # One pending row per provider: a newer upload withdraws the previous pending one.
            for old in mine.filter(status=QrStatus.PENDING):
                old.status = QrStatus.WITHDRAWN
                old.save()
                discard_image(old)
            qr = DonationQr.objects.create(
                account=request.user, provider=d["provider"], account_name=d["account_name"],
                qr_image_url=d["file_url"], status=QrStatus.PENDING, replaces=live)
        return Response({"donation_qr_id": str(qr.pk), "status": qr.status, "verified": qr.verified,
                         "replaces": str(live.pk) if live else None}, status=201)


class DonationQrOfflineView(APIView):
    """DW2 · "Take my QR offline now". A shelter withdraws its OWN live QR at once (a closed or compromised wallet), or cancels its own pending
    replacement. One tap, no password: taking a code down can only reduce what donors see."""
    permission_classes = [IsShelter]

    def post(self, request, donation_qr_id):
        with transaction.atomic():
            qr = (DonationQr.objects.select_for_update()
                  .filter(donation_qr_id=donation_qr_id, account=request.user).first())
            if qr is None:
                return Response({"error": {"code": "not_found", "message": "No such QR"}}, status=404)
            if qr.status not in (QrStatus.LIVE, QrStatus.PENDING):
                return Response({"error": {"code": "not_active", "message": "That QR is not live or pending."}}, status=409)
            qr.status = QrStatus.WITHDRAWN
            qr.decided_at = timezone.now()
            qr.save()
            discard_image(qr)
        return Response(own_repr(qr))


class ShelterDonationQrPublicView(APIView):
    """US-Q2 · the public donate surface's data source. Two-key gate, always both: the
    org must be approved (any approved shelter_org verification — US-X4's rule) AND the
    QR itself reviewer-verified. Either key missing reads as 404, not an empty list —
    there is nothing public to say about an org that hasn't cleared both checks."""
    permission_classes = [AllowAny]

    def get(self, request, account_id):
        # ⚠️ A THIRD KEY, added after the Abot-tulong QR test plan (AT9/AT25): the account must be ACTIVE.
        # Suspending or deleting a shelter changes `account.status` and revokes its sessions, but neither
        # touches the org approval or the QR row, so a suspended or erased shelter's payment QR kept being
        # served to donors. Reads as the same 404 "No such shelter" as any non-org, so it leaks nothing.
        approved = VerificationRequest.objects.filter(
            account_id=account_id, type="shelter_org", status="approved",
            account__status=AccountStatus.ACTIVE).exists()
        if not approved:
            return Response({"error": {"code": "not_found", "message": "No such shelter"}},
                            status=404)
        qrs = list(DonationQr.objects.filter(account_id=account_id, status=QrStatus.LIVE).order_by("provider"))
        if not qrs:
            return Response({"error": {"code": "not_found",
                                       "message": "No verified donation QR on file"}}, status=404)
        return Response({"donation_qrs": [
            {"provider": q.provider, "account_name": q.account_name, "qr_image_url": q.qr_image_url}
            for q in qrs]})


def _profile_repr(p):
    return {"shelter_profile_id": str(p.shelter_profile_id), "org_name": p.org_name,
            "org_type": p.org_type, "tier": p.tier,
            "description": p.description or None, "logo_url": p.logo_url or None,
            "registration_type": p.registration_type,
            "registration_number": p.registration_number or None,
            "official_email": p.official_email or None,
            "operating_hours": p.operating_hours or None,
            "contact_person_name": p.contact_person_name or None,
            "contact_person_role": p.contact_person_role or None,
            "official_phone": p.official_phone or None, "website_url": p.website_url or None,
            "vet_name": p.vet_name or None, "vet_prc_number": p.vet_prc_number or None,
            "donation_instructions": p.donation_instructions}
