"""US-X4 · tier upgrade (community_rescue -> registered_ngo)."""
import json

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from shelter.models import ShelterProfile, ShelterTier
from verifications.models import VerificationDocument, VerificationRequest
from verifications.review import approve_request, reject_request


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _reviewer():
    return AccountFactory(account_type="admin")


def _approved_tier1_shelter():
    """A community_rescue shelter with an APPROVED tier-1 verification (base docs on file)."""
    shelter = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    ShelterProfile.objects.create(account=shelter, org_name="Bantay Hayop", org_type="rescue",
                                  tier=ShelterTier.COMMUNITY_RESCUE)
    vr = VerificationRequest.objects.create(account=shelter, type="shelter_org", status="pending")
    for dt in ["gov_id", "proof_billing", "rescue_photos", "rescue_photos", "rescue_photos"]:
        VerificationDocument.objects.create(verification=vr, doc_type=dt, file_url="s3://base")
    approve_request(vr, _reviewer())
    return shelter


NGO_PAPERS = [{"doc_type": "sec_dti", "file_url": "s3://sec"},
              {"doc_type": "bai_cert", "file_url": "s3://bai"}]


def _upgrade(client, shelter, **body):
    payload = {"consent_version": "v1", "documents": NGO_PAPERS, **body}
    return client.post("/api/v1/verifications/upgrade", json.dumps(payload),
                       content_type="application/json", **_hdr(shelter))


def _tier(shelter):
    return ShelterProfile.objects.get(account=shelter).tier


@pytest.mark.django_db
def test_upgrade_creates_a_pending_request_without_re_uploading_the_base(client):
    shelter = _approved_tier1_shelter()
    res = _upgrade(client, shelter)   # sends only the NGO delta, not the base docs
    assert res.status_code == 201
    assert res.json()["status"] == "pending"
    # tier is NOT moved at submit — the badge can't precede the decision.
    assert _tier(shelter) == ShelterTier.COMMUNITY_RESCUE


@pytest.mark.django_db
def test_approving_the_upgrade_promotes_the_tier(client):
    shelter = _approved_tier1_shelter()
    vid = _upgrade(client, shelter).json()["verification_id"]
    approve_request(VerificationRequest.objects.get(pk=vid), _reviewer())
    assert _tier(shelter) == ShelterTier.REGISTERED_NGO


@pytest.mark.django_db
def test_the_in_flight_upgrade_does_not_revoke_tier1_gates(client):
    shelter = _approved_tier1_shelter()
    _upgrade(client, shelter)   # a new pending shelter_org request now exists
    gates = client.get("/api/v1/shelter/dashboard", **_hdr(shelter)).json()
    assert gates["gates"]["can_publish"] is True          # tier-1 approval still stands
    assert gates["verification"]["status"] == "pending"   # ...while the upgrade is under review


@pytest.mark.django_db
def test_upgrade_requires_an_approved_tier1_first(client):
    shelter = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    ShelterProfile.objects.create(account=shelter, org_name="New Org", org_type="rescue",
                                  tier=ShelterTier.COMMUNITY_RESCUE)
    VerificationRequest.objects.create(account=shelter, type="shelter_org", status="pending")
    res = _upgrade(client, shelter)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "tier1_incomplete"


@pytest.mark.django_db
def test_already_registered_ngo_cannot_upgrade_again(client):
    shelter = _approved_tier1_shelter()
    ShelterProfile.objects.filter(account=shelter).update(tier=ShelterTier.REGISTERED_NGO)
    res = _upgrade(client, shelter)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "already_ngo"


@pytest.mark.django_db
def test_a_second_upgrade_while_one_is_pending_is_refused(client):
    shelter = _approved_tier1_shelter()
    assert _upgrade(client, shelter).status_code == 201
    before = VerificationRequest.objects.filter(account=shelter).count()
    res = _upgrade(client, shelter)   # a double tap on "Upgrade"
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "already_pending"
    assert VerificationRequest.objects.filter(account=shelter).count() == before
    assert VerificationRequest.objects.filter(account=shelter, status="pending").count() == 1


@pytest.mark.django_db
def test_a_rejected_upgrade_can_be_retried(client):
    shelter = _approved_tier1_shelter()
    vid = _upgrade(client, shelter).json()["verification_id"]
    reject_request(VerificationRequest.objects.get(pk=vid), _reviewer(), "SEC papers are blurry")
    res = _upgrade(client, shelter)
    assert res.status_code == 201
    assert res.json()["verification_id"] != vid


@pytest.mark.django_db
def test_missing_ngo_papers_are_rejected(client):
    shelter = _approved_tier1_shelter()
    res = _upgrade(client, shelter, documents=[{"doc_type": "bai_cert", "file_url": "s3://bai"}])
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "missing_docs"
    assert "sec_dti" in res.json()["error"]["required"]


@pytest.mark.django_db
def test_bai_can_be_deferred_at_upgrade(client):
    shelter = _approved_tier1_shelter()
    res = _upgrade(client, shelter, documents=[{"doc_type": "sec_dti", "file_url": "s3://sec"}],
                   bai_pending=True)
    assert res.status_code == 201


@pytest.mark.django_db
def test_non_shelter_cannot_upgrade(client):
    member = AccountFactory(account_type="personal", email_verified_at=timezone.now())
    res = _upgrade(client, member)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "not_shelter"
