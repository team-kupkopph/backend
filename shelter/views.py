from django.db import transaction
from django.db.models import CharField, DateTimeField, F, Value
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Address
from accounts.visibility import public_account_q
from common.storage import PUBLIC, delete_object
from common.throttles import DonationQrUploadThrottle
from common.uploads import own_upload_url_error
from listings.models import AdoptionInquiry, InquiryKind, InquiryStatus, ListingStatus
from shelter.models import (
    AUDITED_PROFILE_FIELDS,
    DonationQr,
    ProfileChangeOutcome,
    QrStatus,
    ShelterProfile,
    ShelterProfileChange,
)
from shelter.permissions import IsShelter
from shelter.qr_rules import discard_image, own_repr
from shelter.serializers import (
    DonationQrUploadSerializer,
    ShelterProfileCreateSerializer,
    ShelterProfilePatchSerializer,
)
from shelter.telemetry import emit_profile_created, emit_profile_updated
from verifications.models import VerificationRequest
from volunteer.models import VolunteerShift, VolunteerSignup, VolunteerType

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
        emit_profile_created(profile)   # SH16
        return Response({"shelter_profile_id": str(profile.shelter_profile_id)}, status=201)

    def get(self, request):
        """T8 · the shelter's own full profile. Before this the Donation instructions screen read
        its own text back through the PUBLIC needs route, and nothing could show the rest."""
        profile = ShelterProfile.objects.filter(account=request.user).first()
        if profile is None:
            return _no_profile()
        return Response(_profile_repr(profile))

    def patch(self, request):
        s = ShelterProfilePatchSerializer(data=request.data, partial=True,
                                          context={"request": request})
        # SH9 · the same 404 as GET for the same condition (it was 409 here).
        if not ShelterProfile.objects.filter(account=request.user).exists():
            return _no_profile()
        s.is_valid(raise_exception=True)
        data = dict(s.validated_data)
        if "logo_file_url" in data:
            data["logo_url"] = data.pop("logo_file_url")

        # SH14b · the lock check and the save are one transaction on the locked row, and the save writes only
        # what changed: an approval landing in between can no longer let a rename through, and two devices
        # editing different fields no longer overwrite each other.
        with transaction.atomic():
            profile = ShelterProfile.objects.select_for_update().filter(account=request.user).first()
            if profile is None:
                return _no_profile()
            verified = request.user.verifications.filter(type="shelter_org", status="approved").exists()
            # The edit form re-sends everything: only a different value is a change.
            changed = sorted(k for k, v in data.items() if getattr(profile, k) != v)
            # AS7 · the org name is what the reviewers verified. Once ANY shelter_org verification is
            # approved it can no longer change here — otherwise a verified shelter could become a
            # different organisation under the same badge.
            if "org_name" in changed and verified:
                _audit(request.user, "org_name", profile.org_name, data["org_name"], ProfileChangeOutcome.REFUSED)
                refused = Response({"error": {
                    "code": "org_name_locked", "field": "org_name",
                    "message": "Your organization's name is what we verified. "
                               "Contact us to change it."}}, status=409)
                return refused      # the attempt's row commits with this transaction: it is the record
            if verified:    # SH14a / SQ3
                for key in changed:
                    if key in AUDITED_PROFILE_FIELDS:
                        _audit(request.user, key, getattr(profile, key), data[key], ProfileChangeOutcome.CHANGED)
            old_logo = profile.logo_url
            for key in changed:
                setattr(profile, key, data[key])
            if changed:
                profile.save(update_fields=changed)
            if "logo_url" in changed:
                _discard_logo(old_logo, request.user)   # SH14c
        emit_profile_updated(changed)   # SH16
        return Response(_profile_repr(profile))


def _no_profile():
    return Response({"error": {"code": "no_profile", "message": "Create the shelter profile first"}}, status=404)


def _audit(account, field, old, new, outcome):
    ShelterProfileChange.objects.create(account=account, field=field, old_value=old or "", new_value=new or "",
                                        outcome=outcome)


def _discard_logo(url, account):
    """SH14c · `shelter_logo` is a PUBLIC bucket kind, so a replaced logo stayed reachable forever. Delete the old
    object once the save commits, and only when it is this account's own upload (never a legacy foreign URL)."""
    if url and own_upload_url_error(url, account, "shelter_logo", noun="logo") is None:
        transaction.on_commit(lambda: delete_object(url, PUBLIC))


class ShelterRequestsView(APIView):
    """B-be2 · GET /shelter/requests — one merged inbox across the shelter's three
    inbound-request surfaces: adoption inquiries on its own listings, volunteer signups
    on its own shifts, and placements addressed to it (a case-worker `.../place`d
    inquiry where the shelter is the *adopter*; see listings/tests/test_inquiries.py).

    SH7 / SH7b · one `UNION ALL` of three narrow projections, filtered by kind and open status,
    ordered `(-created_at, -id)` and paged in SQL. It used to load the shelter's whole history into
    Python and merge it there, so the cost grew with every request ever made, not with the page.
    SH8 · an unknown `kind` or `status` is a 400, not a silent empty list."""
    permission_classes = [IsShelter]

    def get(self, request):
        kind = request.query_params.get("kind")
        status = request.query_params.get("status")
        if kind is not None and kind not in REQUEST_KINDS:
            return _invalid("kind", f"kind must be one of {', '.join(REQUEST_KINDS)}")
        if status is not None and status != "open":
            return _invalid("status", "status must be open, or left out")
        open_only = status == "open"

        parts = [build(request.user, open_only) for k, build in (
            ("adoption", _adoption_rows), ("volunteer", _volunteer_rows), ("placement", _placement_rows),
        ) if kind in (None, k)]
        union = parts[0].union(*parts[1:], all=True) if len(parts) > 1 else parts[0]

        page = _page_number(request)
        start = (page - 1) * REQUESTS_PAGE_SIZE
        rows = list(union.order_by("-ts", "-rid")[start:start + REQUESTS_PAGE_SIZE + 1])
        return Response({
            "results": [_request_item(r) for r in rows[:REQUESTS_PAGE_SIZE]],
            "next": page + 1 if len(rows) > REQUESTS_PAGE_SIZE else None,
            "count": union.count(),
        })


REQUEST_KINDS = ("adoption", "volunteer", "placement")
_ROW = ("k", "rid", "ts", "st", "title", "sub", "sub_at", "sub_type", "tgt")


def _row_values(qs, kind, *, title, sub, sub_at=None, sub_type=None, tgt):
    """One kind's projection onto the shared `_ROW` columns, so the three can `UNION`.
    Every column is an annotation (never a model field) so their order is `_ROW`'s on all three."""
    return qs.annotate(
        k=Value(kind, output_field=CharField()), rid=F("pk"), ts=F("created_at"), st=F("status"),
        title=title, sub=sub,
        sub_at=sub_at or Value(None, output_field=DateTimeField()),
        sub_type=sub_type or Value("", output_field=CharField()),
        tgt=tgt,
    ).values(*_ROW)


def _adoption_rows(shelter, open_only):
    # Everything the shelter posted, minus placements: those are surfaced by `_placement_rows`,
    # keyed off who the *adopter* is.
    qs = (AdoptionInquiry.objects.filter(listing__posted_by=shelter)
          .exclude(kind=InquiryKind.PLACEMENT))
    if open_only:
        qs = qs.filter(status=InquiryStatus.ACTIVE)
    return _row_values(qs, "adoption", title=F("listing__name"),
                       sub=F("adopter_account__display_name"), tgt=F("pk"))


def _volunteer_rows(shelter, open_only):
    # SH4 · "open" is `awaiting_decision` (the shift has not ended), the same rule as the
    # dashboard's `pending_requests`. A request for a finished shift is history, not open.
    qs = (VolunteerSignup.objects.awaiting_decision(shelter) if open_only
          else VolunteerSignup.objects.filter(shift__shelter_account=shelter))
    return _row_values(qs, "volunteer", title=F("volunteer_account__display_name"),
                       sub=F("shift__title"), sub_at=F("shift__starts_at"), sub_type=F("shift__type"),
                       tgt=F("shift_id"))


def _placement_rows(shelter, open_only):
    # AD22 · a placement is said by its `kind` column, never inferred from the stage ladder.
    qs = AdoptionInquiry.objects.filter(adopter_account=shelter, kind=InquiryKind.PLACEMENT)
    if open_only:
        qs = qs.filter(status=InquiryStatus.ACTIVE)
    return _row_values(qs, "placement", title=F("listing__name"),
                       sub=F("listing__posted_by__display_name"), tgt=F("pk"))


_VOLUNTEER_TYPE_LABEL = dict(VolunteerType.choices)


def _request_item(r):
    kind = r["k"]
    if kind == "volunteer":
        subtitle = (f"{timezone.localtime(r['sub_at']):%a, %b %-d} · "
                    f"{r['sub'] or _VOLUNTEER_TYPE_LABEL.get(r['sub_type'], r['sub_type'])}")
        route = "shelterVolunteerRequests"
    else:
        subtitle = f"Placed by {r['sub']}" if kind == "placement" else r["sub"]
        route = "inquiry"
    return {
        "kind": kind, "id": str(r["rid"]), "title": r["title"], "subtitle": subtitle,
        "status": r["st"], "created_at": r["ts"].isoformat(),
        "target": {"route": route, "id": str(r["tgt"])},
    }


def _invalid(field, message):
    return Response({"error": {"code": "invalid", "field": field, "message": message}}, status=400)


def _page_number(request):
    try:
        return max(1, int(request.query_params.get("page") or 1))
    except (TypeError, ValueError):
        return 1


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
            "pending_requests": VolunteerSignup.objects.awaiting_decision(request.user, now=now).count(),
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
        ).filter(public_account_q("account__")).exists()
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
