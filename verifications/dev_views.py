"""Dev-only endpoint for seeding a pending shelter verification without walking the
submission form.

`POST /api/v1/me/verifications/dev/seed` creates a pending `shelter_org`
VerificationRequest, with a plausible pre-baked document set, on behalf of the
calling (authenticated) shelter account. It exists purely so automated and manual
test runs can reach the "submitted, pending review" state on a simulator/emulator
without typing form-field text — see dev/test-plan-auth.md's C-2 (tier-2 happy path)
and C-3 (tier-1) rows.

Sibling to accounts/dev_views.py::dev_seed_tokens (PR #38): same DEBUG-gate rule —
settings.DEBUG is checked FIRST, and the failure is a bare Http404, so this must be
invisible in production. Unlike the auth-side sibling this endpoint acts on the
caller's own account, so it requires a valid JWT rather than being wide open. That
means the DEBUG check has to happen in `dispatch()`, *before* DRF's own initial()
(auth + permissions) runs — otherwise a DEBUG=False request without a token would
hit IsAuthenticated first and 401 instead of 404, and one WITH a token would reach
DRF's exception handler and get JSON-wrapped instead of the bare Django 404 an
unmatched URL produces. Routing the check through dispatch() (outside DRF's
try/except in APIView.dispatch) keeps a DEBUG=False response byte-for-byte
identical to hitting an unknown URL, the same guarantee
test_debug_off_hides_the_route_entirely established for the auth-side sibling.
"""
from django.conf import settings
from django.http import Http404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from shelter.models import ShelterProfile
from verifications.models import VerificationDocument, VerificationRequest
from verifications.views import _verification_repr

# Kind -> pre-baked fixture. Both kinds produce a VerificationRequest with
# type="shelter_org" — VerificationType has no separate "tier-1" value; the model
# distinguishes tiers only by which documents are on file (verifications/rules.py)
# and, for an org, the ShelterProfile.vet_name/vet_prc_number fields (tier-2 only).
# "kind" is this endpoint's own vocabulary for *which flavor* to fabricate, matching
# dev/test-plan-auth.md's C-3 (tier-1) vs C-2 (tier-2 "happy path") rows:
#   - "shelter_tier1" -> the tier-1 base set alone, satisfying
#     rules.required_doc_types("community_rescue").
#   - "shelter_org"   -> the tier-1 base plus the NGO extras (SEC/DTI + BAI), the full
#     set rules.required_doc_types("registered_ngo") requires, plus the vet fields.
_TIER1_DOCS = [
    {"doc_type": "gov_id", "file_url": "s3://dev-seed/gov_id.jpg"},
    {"doc_type": "proof_billing", "file_url": "s3://dev-seed/proof_billing.jpg"},
    {"doc_type": "rescue_photos", "file_url": "s3://dev-seed/rescue_photo_1.jpg"},
    {"doc_type": "rescue_photos", "file_url": "s3://dev-seed/rescue_photo_2.jpg"},
    {"doc_type": "rescue_photos", "file_url": "s3://dev-seed/rescue_photo_3.jpg"},
]
_TIER2_EXTRA_DOCS = [
    {"doc_type": "sec_dti", "file_url": "s3://dev-seed/sec_dti.pdf"},
    {"doc_type": "bai_cert", "file_url": "s3://dev-seed/bai_cert.pdf"},
]
_VET_NAME = "Dr. Ana Dela Cruz"
_VET_PRC_NUMBER = "654321"   # 6-8 digits — shelter/serializers.py::PRC_RE


def _kind_fixture(kind):
    """(documents, social_proof_url, shelter_profile_fields) for a known `kind`, or
    None for anything else.

    `shelter_profile_fields` entries are only written when the current column value is
    blank (see the dispatch loop below) — repeated seed calls don't overwrite state a
    real caller might have set, EXCEPT for `tier`: `shelter_org` bumps a tier-1 profile
    to `registered_ngo` because C-2's UI walk assumes the shelter's tier is already
    tier-2 by the time NGO papers land, and a mismatch between the pending
    verification's document set and `ShelterProfile.tier` breaks the shelter shell's
    dashboard state derivation (F21)."""
    if kind == "shelter_tier1":
        return list(_TIER1_DOCS), "https://facebook.com/dev-seed-shelter", {}
    if kind == "shelter_org":
        return (list(_TIER1_DOCS) + list(_TIER2_EXTRA_DOCS), "",
               {"vet_name": _VET_NAME, "vet_prc_number": _VET_PRC_NUMBER,
                "tier": "registered_ngo"})
    return None


class MeVerificationsSeedView(APIView):
    permission_classes = [IsAuthenticated]

    def dispatch(self, request, *args, **kwargs):
        if not settings.DEBUG:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        kind = request.data.get("kind") if isinstance(request.data, dict) else None
        fixture = _kind_fixture(kind)
        if fixture is None:
            return Response({"detail": "kind must be shelter_org or shelter_tier1"}, status=400)
        documents, social_proof_url, profile_fields = fixture

        profile = ShelterProfile.objects.filter(account=request.user).first()
        if profile is None:
            return Response({"detail": "caller is not a shelter account"}, status=400)

        # Only one pending shelter_org request at a time — the same constraint the real
        # submit path enforces (verifications/views.py::VerificationCreateView), since
        # BOTH kinds map internally to type="shelter_org" (the model has no tier-1 vs.
        # tier-2 distinction on the type field). F22: the 409 body names the model
        # type, not the requested `kind`, because echoing the caller's kind was
        # misleading when the pending it collided with was created with the other kind.
        if request.user.verifications.filter(type="shelter_org", status="pending").exists():
            return Response({"detail": "a pending shelter_org verification already exists"}, status=409)

        # `tier` gets its own rule (F21): it is a required field with a default, never
        # blank, so the blank-guard below would skip it. For `shelter_org` we want to
        # bump `community_rescue` to `registered_ngo`; leave any other value alone (a
        # profile already at `registered_ngo` is idempotent; any future third value is
        # left to the caller to reason about).
        update_fields = []
        for f, v in profile_fields.items():
            current = getattr(profile, f)
            if f == "tier":
                if current == "community_rescue" and v == "registered_ngo":
                    setattr(profile, f, v)
                    update_fields.append(f)
                continue
            if not current:
                setattr(profile, f, v)
                update_fields.append(f)
        if update_fields:
            profile.save(update_fields=update_fields)

        vr = VerificationRequest.objects.create(
            account=request.user, type="shelter_org", status="pending",
            social_proof_url=social_proof_url, consent_at=timezone.now(),
            consent_version="dev-seed-v1")
        for doc in documents:
            VerificationDocument.objects.create(
                verification=vr, doc_type=doc["doc_type"], file_url=doc["file_url"],
                status="pending")

        return Response(_verification_repr(vr), status=201)
