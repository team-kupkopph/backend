import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


@pytest.mark.django_db
def test_submit_verified_member_creates_request_doc_capability_and_persists_consent(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.post("/api/v1/verifications", {
        "type": "rescuer", "social_proof_url": "https://facebook.com/ana",
        "consent_version": "2026-08-01",
        "documents": [{"doc_type": "gov_id", "file_url": "https://x/gov.jpg"}],
    }, content_type="application/json", **_hdr(acc))
    assert res.status_code == 201
    assert res.json()["status"] == "pending"
    from verifications.models import AccountCapability, VerificationRequest
    vr = VerificationRequest.objects.get(account=acc, type="rescuer")
    assert vr.consent_at is not None and vr.consent_version == "2026-08-01"
    assert vr.documents.filter(doc_type="gov_id").exists()
    cap = AccountCapability.objects.get(account=acc, capability="rescuer")
    assert cap.status == "pending"


@pytest.mark.django_db
def test_submit_without_consent_returns_422(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.post("/api/v1/verifications", {
        "type": "rescuer", "social_proof_url": "https://facebook.com/ana",
        "documents": [{"doc_type": "gov_id", "file_url": "https://x/gov.jpg"}],
    }, content_type="application/json", **_hdr(acc))
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "consent_missing"


@pytest.mark.django_db
def test_presign_returns_a_file_url(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.post("/api/v1/media/presign",
                      {"purpose": "verification_doc", "content_type": "image/jpeg"},
                      content_type="application/json", **_hdr(acc))
    assert res.status_code == 200 and res.json()["file_url"]
