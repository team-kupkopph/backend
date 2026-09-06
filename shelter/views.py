from django.db import transaction
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Address
from shelter.models import DonationQr, ShelterProfile
from shelter.permissions import IsShelter
from shelter.serializers import (
    DonationQrUploadSerializer,
    ShelterProfileCreateSerializer,
    ShelterProfilePatchSerializer,
)
from verifications.models import VerificationRequest


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

    def patch(self, request):
        profile = ShelterProfile.objects.filter(account=request.user).first()
        if profile is None:
            return Response({"error": {"code": "no_profile",
                                       "message": "Create the shelter profile first"}}, status=409)
        s = ShelterProfilePatchSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        for key, value in s.validated_data.items():
            setattr(profile, key, value)
        profile.save()
        return Response(_profile_repr(profile))


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
        draft_listings = request.user.listings.count()   # unverified: everything they post is a draft
        # US-X3 · donations are a TWO-key gate: org approved AND a reviewer-verified QR on file.
        # Still fully derived (§3.5) — no stored donations flag; the QR's `verified` is the check.
        donations_enabled = approved and request.user.donation_qrs.filter(verified=True).exists()
        return Response({
            "verification": {"submitted": submitted,
                             "status": vr.status if vr else None, "docs": docs},
            "counts": {"draft_listings": draft_listings, "adopted": 0, "donations": 0},
            "gates": {"can_publish": approved, "donations_enabled": donations_enabled},
        })


class DonationQrView(APIView):
    """US-Q1 · upload (or replace) a donation QR. One row per (account, provider) — a
    second POST for the same provider is an edit, not a second QR. Uploading is not
    gated on the org's own approval (decision 2's draft-first pattern, same as listings)
    — the public gate (US-Q2) is the separate, always-both-keys check."""
    permission_classes = [IsShelter]

    def post(self, request):
        s = DonationQrUploadSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        # ⚠️ Any edit resets `verified` — the obvious fraud is swapping the image after
        # a reviewer already checked it. This endpoint's only job is "submit an image
        # for this provider," so every call here is by definition a new image to check.
        # No unique(account, provider) constraint exists in the DDL, so this looks up
        # the latest row rather than relying on get_or_create's implicit uniqueness.
        qr = (DonationQr.objects.filter(account=request.user, provider=d["provider"])
              .order_by("-created_at").first())
        if qr is not None:
            qr.account_name = d["account_name"]
            qr.qr_image_url = d["file_url"]
            qr.verified = False
            qr.save(update_fields=["account_name", "qr_image_url", "verified"])
        else:
            qr = DonationQr.objects.create(
                account=request.user, provider=d["provider"], account_name=d["account_name"],
                qr_image_url=d["file_url"], verified=False)
        return Response({"donation_qr_id": str(qr.pk), "verified": qr.verified}, status=201)


class ShelterDonationQrPublicView(APIView):
    """US-Q2 · the public donate surface's data source. Two-key gate, always both: the
    org must be approved (any approved shelter_org verification — US-X4's rule) AND the
    QR itself reviewer-verified. Either key missing reads as 404, not an empty list —
    there is nothing public to say about an org that hasn't cleared both checks."""
    permission_classes = [AllowAny]

    def get(self, request, account_id):
        approved = VerificationRequest.objects.filter(
            account_id=account_id, type="shelter_org", status="approved").exists()
        if not approved:
            return Response({"error": {"code": "not_found", "message": "No such shelter"}},
                            status=404)
        qrs = list(DonationQr.objects.filter(account_id=account_id, verified=True))
        if not qrs:
            return Response({"error": {"code": "not_found",
                                       "message": "No verified donation QR on file"}}, status=404)
        return Response({"donation_qrs": [
            {"provider": q.provider, "account_name": q.account_name, "qr_image_url": q.qr_image_url}
            for q in qrs]})


def _profile_repr(p):
    return {"shelter_profile_id": str(p.shelter_profile_id), "org_name": p.org_name,
            "org_type": p.org_type, "tier": p.tier,
            "contact_person_name": p.contact_person_name or None,
            "contact_person_role": p.contact_person_role or None,
            "official_phone": p.official_phone or None, "website_url": p.website_url or None,
            "vet_name": p.vet_name or None, "vet_prc_number": p.vet_prc_number or None}
