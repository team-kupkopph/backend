import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.media import policy_for
from common.storage import create_presigned_upload
from common.throttles import MediaPresignThrottle
from verifications.models import AccountCapability, VerificationDocument, VerificationRequest
from verifications.rules import MIN_RESCUE_PHOTOS, base_complete, missing_docs, required_doc_types
from verifications.serializers import PresignSerializer, VerificationSerializer


def _shelter_doc_error(tier, docs, bai_pending):
    """Server-derived doc check (never trust the client's list). Returns an
    (http_status, body) tuple to reject, or None if the set satisfies the tier. The
    tier->doc-set rule itself lives in verifications/rules.py, shared with the reviewer's
    missing-docs line (US-R4) so the two can't drift."""
    types = [d["doc_type"] for d in docs]
    if not base_complete(types):
        # Rule 5: the tier-1 base must be present before any tier-2 evidence is accepted.
        if tier == "registered_ngo":
            return 409, {"error": {"code": "tier1_incomplete",
                                   "message": "Submit the base (tier-1) documents first"}}
        return 422, {"error": {"code": "missing_docs", "message": "Required documents missing",
                               "details": {"required": ["gov_id", "proof_billing", "rescue_photos"],
                                           "min_photos": MIN_RESCUE_PHOTOS}}}
    if tier == "registered_ngo" and missing_docs(tier, types, bai_pending):
        required = [t for t in required_doc_types(tier)
                    if not (t == "bai_cert" and bai_pending)]
        return 422, {"error": {"code": "missing_docs", "message": "Required documents missing",
                               "details": {"required": required, "min_photos": MIN_RESCUE_PHOTOS}}}
    return None


def _document_repr(d):
    return {"document_id": str(d.document_id), "doc_type": d.doc_type,
            "status": d.status, "review_note": d.review_note or None,
            "superseded_by": str(d.superseded_by_id) if d.superseded_by_id else None}


def _verification_repr(vr):
    return {"verification_id": str(vr.verification_id), "type": vr.type,
            "status": vr.status, "notes": vr.notes or None,
            "submitted_at": vr.submitted_at.isoformat(),
            "reviewed_at": vr.reviewed_at.isoformat() if vr.reviewed_at else None,
            "documents": [_document_repr(d) for d in vr.documents.order_by("uploaded_at")]}


class MeVerificationsView(APIView):
    """US-V2 · the applicant's document tracker — their own requests and every file's
    per-file state, so they can see exactly what to fix. Read-only."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = request.user.verifications.order_by("-submitted_at")
        return Response({"verifications": [_verification_repr(vr) for vr in qs]})


class ResubmitDocumentView(APIView):
    """US-V3 · replace a rejected file. The old row is SUPERSEDED, never deleted or
    mutated (audit trail), the replacement is inserted pending, and the request returns to
    the queue. Only the owner, and only a genuinely rejected file — so this can't become a
    way to swap an approved ID after the fact."""
    permission_classes = [IsAuthenticated]

    def post(self, request, verification_id):
        vr = VerificationRequest.objects.filter(pk=verification_id).first()
        if vr is None:
            return Response({"error": {"code": "not_found",
                                       "message": "No such verification request"}}, status=404)
        if vr.account_id != request.user.account_id:
            return Response({"error": {"code": "forbidden",
                                       "message": "Not your verification request"}}, status=403)
        old = VerificationDocument.objects.filter(
            verification=vr, document_id=request.data.get("replaces")).first()
        if old is None:
            return Response({"error": {"code": "not_found",
                                       "message": "No such document on this request"}}, status=404)
        if old.status != "rejected":
            return Response({"error": {"code": "not_replaceable",
                                       "message": "Only a rejected file can be replaced"}}, status=409)
        file_url = (request.data.get("file_url") or "").strip()
        if not file_url:
            return Response({"error": {"code": "invalid", "message": "file_url is required"}},
                            status=422)
        with transaction.atomic():
            new = VerificationDocument.objects.create(
                verification=vr, doc_type=request.data.get("doc_type") or old.doc_type,
                file_url=file_url, status="pending")
            old.superseded_by = new
            old.save(update_fields=["superseded_by"])
            vr.status = "pending"
            vr.save(update_fields=["status"])
        return Response({"document_id": str(new.document_id)}, status=201)


class PresignView(APIView):
    """US-D2 · `key` is always server-chosen (`{purpose}/{account_id}/{uuid}`) — never a
    client-supplied path, so a caller can't overwrite or guess another account's object.
    `content_type`/size are checked against `purpose`'s policy here (a clean 422 before
    any AWS call) AND passed to S3 as presigned-POST conditions (harder to bypass) — see
    `common/media.py::PURPOSES` and `common/storage.py::create_presigned_upload`. No
    bucket configured (the current dev slice) still returns real `example.invalid`
    placeholders, same shape as before this story."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [MediaPresignThrottle]   # US-K2 · upload credentials, per account

    def post(self, request):
        s = PresignSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        purpose, content_type = s.validated_data["purpose"], s.validated_data["content_type"]

        policy = policy_for(purpose)
        if policy is None:
            return Response({"error": {"code": "purpose_unknown",
                                       "message": f"Unknown upload purpose: {purpose}"}}, status=422)
        visibility, allowed_types, max_bytes = policy
        if content_type not in allowed_types:
            return Response({"error": {"code": "bad_content_type",
                                       "message": f"{content_type} isn't allowed for {purpose}"}},
                            status=422)

        key = f"{purpose}/{request.user.account_id}/{uuid.uuid4()}"
        return Response(create_presigned_upload(visibility, key, content_type, max_bytes))


class VerificationCreateView(APIView):
    permission_classes = [IsAuthenticated]

    CAP_FOR_TYPE = {"rescuer": "rescuer", "provider": "provider"}

    def post(self, request):
        s = VerificationSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        if not data.get("consent_version"):
            return Response({"error": {"code": "consent_missing",
                                       "message": "Consent is required to submit documents"}},
                            status=422)
        if data["type"] == "shelter_org":
            if request.user.account_type != "shelter":
                return Response({"error": {"code": "not_shelter",
                                           "message": "Only shelter accounts submit org verification"}},
                                status=403)
            from shelter.models import ShelterProfile
            profile = ShelterProfile.objects.filter(account=request.user).first()
            if profile is None:
                return Response({"error": {"code": "no_profile",
                                           "message": "Set up the shelter profile first"}}, status=409)
            # Required set is derived from the stored tier, server-side (§3.5) — the
            # client's document list is never trusted to declare which tier it is.
            err = _shelter_doc_error(profile.tier, data["documents"], data.get("bai_pending", False))
            if err is not None:
                return Response(err[1], status=err[0])
        with transaction.atomic():
            vr = VerificationRequest.objects.create(
                account=request.user, type=data["type"], status="pending",
                social_proof_url=data.get("social_proof_url", ""),
                consent_at=timezone.now(), consent_version=data["consent_version"])
            for doc in data["documents"]:
                VerificationDocument.objects.create(verification=vr, doc_type=doc["doc_type"],
                                                    file_url=doc["file_url"], status="pending")
            cap = self.CAP_FOR_TYPE.get(data["type"])
            if cap:
                AccountCapability.objects.get_or_create(
                    account=request.user, capability=cap, defaults={"status": "pending"})
        return Response({"verification_id": str(vr.verification_id), "status": "pending"},
                        status=201)


class ShelterUpgradeView(APIView):
    """US-X4 · upgrade a tier-1 (community_rescue) shelter to registered_ngo.

    Enforces the tier1 -> tier2 order server-side: an APPROVED tier-1 shelter_org request must
    already exist, and its approved base evidence counts as on-file, so it is NOT re-uploaded —
    the applicant sends only the NGO delta (sec_dti, plus bai_cert unless bai_pending). This
    creates a fresh pending shelter_org request carrying the NGO papers; approving it in the
    normal reviewer queue promotes the tier (see review.approve_request). The tier is never
    moved here — only on approval — so the badge can't precede the decision.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from shelter.models import ShelterProfile, ShelterTier
        if request.user.account_type != "shelter":
            return Response({"error": {"code": "not_shelter",
                                       "message": "Only shelter accounts can upgrade"}}, status=403)
        profile = ShelterProfile.objects.filter(account=request.user).first()
        if profile is None:
            return Response({"error": {"code": "no_profile",
                                       "message": "Set up the shelter profile first"}}, status=409)
        if profile.tier == ShelterTier.REGISTERED_NGO:
            return Response({"error": {"code": "already_ngo",
                                       "message": "This organisation is already a registered NGO"}},
                            status=409)
        tier1 = (request.user.verifications.filter(type="shelter_org", status="approved")
                 .order_by("-submitted_at").first())
        if tier1 is None:
            return Response({"error": {"code": "tier1_incomplete",
                                       "message": "Complete tier-1 verification before upgrading"}},
                            status=409)

        s = VerificationSerializer(data={**request.data, "type": "shelter_org"})
        s.is_valid(raise_exception=True)
        data = s.validated_data
        if not data.get("consent_version"):
            return Response({"error": {"code": "consent_missing",
                                       "message": "Consent is required to submit documents"}},
                            status=422)

        # The tier-1 base already on file counts — union the approved tier-1 request's doc types
        # (the whole request was accepted; per-file status is a separate R6 concern) with the
        # freshly-submitted NGO papers, then apply the SAME tier-2 rule (rules.missing_docs), so
        # the shelter never re-uploads gov_id / proof_billing / rescue_photos.
        on_file = [d.doc_type for d in tier1.documents.all()]
        present = on_file + [d["doc_type"] for d in data["documents"]]
        missing = missing_docs("registered_ngo", present, data.get("bai_pending", False))
        if missing:
            return Response({"error": {"code": "missing_docs",
                                       "message": "Missing required NGO documents",
                                       "required": missing}}, status=422)

        with transaction.atomic():
            vr = VerificationRequest.objects.create(
                account=request.user, type="shelter_org", status="pending",
                social_proof_url=data.get("social_proof_url", ""),
                consent_at=timezone.now(), consent_version=data["consent_version"])
            for doc in data["documents"]:
                VerificationDocument.objects.create(verification=vr, doc_type=doc["doc_type"],
                                                    file_url=doc["file_url"], status="pending")
        return Response({"verification_id": str(vr.verification_id), "status": "pending"},
                        status=201)
