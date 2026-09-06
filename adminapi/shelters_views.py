"""Shelters and donation QRs (US-S1, US-Q1)."""
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from rest_framework.response import Response

from adminapi.pagination import decode_cursor, paginate
from adminapi.permissions import admin_account_for
from adminapi.verifications_views import StaffView
from listings.models import AdoptionListing
from shelter.models import DonationQr, ShelterProfile, ShelterTier
from verifications.models import VerificationRequest


def _approved_shelter_org():
    """⚠️ 'Verified' is DERIVED, never stored.

    It means: there exists a `verification_request(type='shelter_org', status='approved')` for
    the account. "Verified is always derived, never a stored boolean" is load-bearing here
    (Sprint 1 conventions, Sprint 2 rule 1, Decision B) — and `verifications/models.py` records
    that a denormalised column was DELIBERATELY declined in favour of
    idx_verification_acct_type_st. Do not add one for the console's convenience.
    """
    return VerificationRequest.objects.filter(
        account=OuterRef("account"), type="shelter_org", status="approved")


def shelter_row(profile):
    return {
        "shelter_profile_id": str(profile.shelter_profile_id),
        "account_id": str(profile.account_id),
        "org_name": profile.org_name,
        "org_type": profile.org_type,
        "tier": profile.tier,
        "verified": profile.is_verified,
        "is_escalation_partner": profile.is_escalation_partner,
    }


def shelter_detail(profile):
    verifications = list(VerificationRequest.objects
                         .filter(account=profile.account, type="shelter_org")
                         .order_by("-submitted_at"))
    qrs = list(DonationQr.objects.filter(account=profile.account))
    listing_count = AdoptionListing.objects.filter(posted_by=profile.account).count()
    verified = any(v.status == "approved" for v in verifications)

    return {
        **shelter_row(profile),
        "contact_person_name": profile.contact_person_name,
        "official_email": profile.official_email,
        "official_phone": profile.official_phone,
        "website_url": profile.website_url,
        "vet_prc_number": profile.vet_prc_number,
        "listing_count": listing_count,
        "verifications": [{"verification_id": str(v.verification_id), "status": v.status,
                           "submitted_at": v.submitted_at.isoformat()} for v in verifications],
        "donation_qrs": [{"donation_qr_id": str(q.donation_qr_id), "provider": q.provider,
                          "account_name": q.account_name, "verified": q.verified} for q in qrs],
        # ⚠️ Decision B's gating table, rendered as data rather than left for a reviewer to
        # infer. This is what lets them answer "why can't this shelter receive donations?"
        # without reading the spec — and each `blocked_by` names the ONE thing to fix.
        "capabilities": {
            "listings_public": {
                "enabled": verified,
                "blocked_by": None if verified else "The org is not verified yet.",
            },
            "receives_inquiries": {
                "enabled": verified,
                "blocked_by": None if verified else "The org is not verified yet.",
            },
            "donations_enabled": {
                # TWO independent keys. Showing only the QR flag is how a QR gets marked
                # verified while donations stay off and nobody can explain why.
                "enabled": verified and any(q.verified for q in qrs),
                "blocked_by": (
                    "The org is not verified yet." if not verified
                    else "No donation QR yet." if not qrs
                    else "The donation QR is not verified yet." if not any(q.verified for q in qrs)
                    else None
                ),
            },
            "trust_badge": {
                "enabled": verified,
                "blocked_by": None if verified else "The org is not verified yet.",
            },
        },
    }


class ShelterQueueView(StaffView):
    """GET /admin-api/shelters?verified=&tier=&q=&cursor="""

    def get(self, request):
        verified = request.query_params.get("verified")
        tier = request.query_params.get("tier")
        query = (request.query_params.get("q") or "").strip()

        qs = ShelterProfile.objects.annotate(is_verified=Exists(_approved_shelter_org()))
        if verified in ("true", "false"):
            qs = qs.filter(is_verified=(verified == "true"))
        if tier:
            if tier not in ShelterTier.values:
                return Response({"error": {"code": "invalid_tier"}}, status=400)
            qs = qs.filter(tier=tier)
        if query:
            qs = qs.filter(Q(org_name__icontains=query) | Q(official_email__icontains=query))

        qs = qs.order_by("org_name", "shelter_profile_id")
        rows, next_cursor = paginate(qs, decode_cursor(request.query_params.get("cursor", "")),
                                     ["org_name", "shelter_profile_id"])
        return Response({"results": [shelter_row(p) for p in rows], "next_cursor": next_cursor})


class ShelterDetailView(StaffView):
    def get(self, request, shelter_profile_id):
        profile = (ShelterProfile.objects
                   .annotate(is_verified=Exists(_approved_shelter_org()))
                   .filter(shelter_profile_id=shelter_profile_id).first())
        if profile is None:
            return Response({"error": {"code": "not_found"}}, status=404)
        return Response(shelter_detail(profile))


# -- US-Q1 · donation QRs ------------------------------------------------------------------
class DonationQrQueueView(StaffView):
    """GET /admin-api/donation-qrs?verified=&cursor="""

    def get(self, request):
        verified = request.query_params.get("verified")
        qs = DonationQr.objects.select_related("account")
        if verified in ("true", "false"):
            qs = qs.filter(verified=(verified == "true"))
        qs = qs.order_by("created_at", "donation_qr_id")
        rows, next_cursor = paginate(qs, decode_cursor(request.query_params.get("cursor", "")),
                                     ["created_at", "donation_qr_id"])
        return Response({"results": [self.row(q) for q in rows], "next_cursor": next_cursor})

    @staticmethod
    def row(qr):
        org_verified = VerificationRequest.objects.filter(
            account=qr.account, type="shelter_org", status="approved").exists()
        return {
            "donation_qr_id": str(qr.donation_qr_id),
            "provider": qr.provider,
            "account_name": qr.account_name,
            "qr_image_url": qr.qr_image_url,
            "verified": qr.verified,
            "created_at": qr.created_at.isoformat(),
            "account": {"account_id": str(qr.account.account_id),
                        "display_name": qr.account.display_name},
            # ⚠️ BOTH gates, always. The public donate path needs the org approved AND the QR
            # verified; a reviewer shown only the second cannot explain why donations are still
            # off after they verified the QR.
            "org_verified": org_verified,
            "donations_live": org_verified and qr.verified,
        }


class DonationQrDecisionView(StaffView):
    verify = None

    def post(self, request, donation_qr_id):
        notes = (request.data.get("notes") or "").strip()
        if not self.verify and not notes:
            # Taking a live donation channel down is the action most likely to be questioned.
            return Response({"error": {"code": "reason_required"}}, status=422)

        with transaction.atomic():
            qr = DonationQr.objects.select_for_update().filter(donation_qr_id=donation_qr_id).first()
            if qr is None:
                return Response({"error": {"code": "not_found"}}, status=404)
            if qr.verified == self.verify:
                return Response({"error": {"code": "already_decided",
                                           "verified": qr.verified}}, status=409)
            qr.verified = self.verify
            qr.save(update_fields=["verified"])

        request._audit_body = {"notes": notes}
        qr.refresh_from_db()
        return Response(DonationQrQueueView.row(qr))


class VerifyQrView(DonationQrDecisionView):
    verify = True


class UnverifyQrView(DonationQrDecisionView):
    verify = False
