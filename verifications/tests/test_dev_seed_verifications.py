"""F16 · POST /api/v1/me/verifications/dev/seed — dev-only shortcut that seeds a
pending shelter verification without walking the submission form, so C-2 (tier-2
happy path) and C-3 (tier-1) in dev/test-plan-auth.md can be walked on the sim
without typing form-field text. Sibling to
accounts/tests/test_dev_seed_tokens.py."""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from shelter.models import ShelterProfile, ShelterTier
from verifications.models import VerificationRequest

URL = "/api/v1/me/verifications/dev/seed"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter():
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    ShelterProfile.objects.create(account=acc, org_name="Bantay Hayop", org_type="rescue",
                                  tier=ShelterTier.COMMUNITY_RESCUE)
    return acc


@pytest.mark.django_db
def test_debug_off_hides_the_route_entirely(client, settings):
    """In prod (DEBUG=False) this must be indistinguishable from an unknown URL:
    same status, no serializer output, no hint the route exists — even
    unauthenticated, since the DEBUG gate must precede the JWT check."""
    settings.DEBUG = False

    res = client.post(URL, {"kind": "shelter_tier1"}, content_type="application/json")
    unknown = client.post("/api/v1/me/verifications/dev/this-route-does-not-exist",
                          {"kind": "shelter_tier1"}, content_type="application/json")

    assert res.status_code == 404
    assert unknown.status_code == 404
    assert res.content == unknown.content


@pytest.mark.django_db
def test_debug_on_unauthenticated_is_401(client, settings):
    settings.DEBUG = True

    res = client.post(URL, {"kind": "shelter_tier1"}, content_type="application/json")

    assert res.status_code == 401


@pytest.mark.django_db
def test_debug_on_personal_account_is_400(client, settings):
    settings.DEBUG = True
    personal = AccountFactory(account_type="personal", email_verified_at=timezone.now())

    res = client.post(URL, {"kind": "shelter_tier1"}, content_type="application/json",
                      **_hdr(personal))

    assert res.status_code == 400
    assert "shelter" in res.json()["detail"]


@pytest.mark.django_db
def test_debug_on_shelter_tier1_seeds_a_pending_request(client, settings):
    settings.DEBUG = True
    shelter = _shelter()

    res = client.post(URL, {"kind": "shelter_tier1"}, content_type="application/json",
                      **_hdr(shelter))

    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "pending"
    assert body["type"] == "shelter_org"
    doc_types = sorted(d["doc_type"] for d in body["documents"])
    assert doc_types == sorted(["gov_id", "proof_billing",
                                "rescue_photos", "rescue_photos", "rescue_photos"])

    listed = client.get("/api/v1/me/verifications", **_hdr(shelter)).json()["verifications"]
    assert len(listed) == 1
    assert listed[0]["verification_id"] == body["verification_id"]


@pytest.mark.django_db
def test_debug_on_shelter_org_seeds_the_full_tier2_docset(client, settings):
    settings.DEBUG = True
    shelter = _shelter()

    res = client.post(URL, {"kind": "shelter_org"}, content_type="application/json",
                      **_hdr(shelter))

    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "pending"
    assert body["type"] == "shelter_org"
    doc_types = {d["doc_type"] for d in body["documents"]}
    assert doc_types == {"gov_id", "proof_billing", "rescue_photos", "sec_dti", "bai_cert"}

    profile = ShelterProfile.objects.get(account=shelter)
    assert profile.vet_name
    assert profile.vet_prc_number

    listed = client.get("/api/v1/me/verifications", **_hdr(shelter)).json()["verifications"]
    assert len(listed) == 1
    assert listed[0]["verification_id"] == body["verification_id"]


@pytest.mark.django_db
def test_debug_on_existing_pending_same_kind_is_409(client, settings):
    settings.DEBUG = True
    shelter = _shelter()
    first = client.post(URL, {"kind": "shelter_tier1"}, content_type="application/json",
                        **_hdr(shelter))
    assert first.status_code == 201

    res = client.post(URL, {"kind": "shelter_tier1"}, content_type="application/json",
                      **_hdr(shelter))

    assert res.status_code == 409
    assert res.json() == {"detail": "already has a pending shelter_tier1 verification"}
    assert VerificationRequest.objects.filter(account=shelter).count() == 1


@pytest.mark.django_db
def test_debug_on_invalid_kind_is_400(client, settings):
    settings.DEBUG = True
    shelter = _shelter()

    res = client.post(URL, {"kind": "bogus"}, content_type="application/json", **_hdr(shelter))

    assert res.status_code == 400
    assert res.json() == {"detail": "kind must be shelter_org or shelter_tier1"}


@pytest.mark.django_db
def test_debug_on_get_is_405(client, settings):
    settings.DEBUG = True
    shelter = _shelter()

    res = client.get(URL, **_hdr(shelter))

    assert res.status_code == 405
