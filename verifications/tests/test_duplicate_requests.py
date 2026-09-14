import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from verifications.models import AccountCapability, VerificationRequest

BODY = {"type": "rescuer", "social_proof_url": "https://facebook.com/ana",
        "consent_version": "2026-08-01",
        "documents": [{"doc_type": "gov_id", "file_url": "https://example.invalid/id.jpg"}]}


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _owner():
    return AccountFactory(account_type="personal", email_verified_at=timezone.now())


@pytest.mark.django_db
def test_second_pending_rescuer_request_is_409(client):
    acc = _owner()
    assert client.post("/api/v1/verifications", BODY, content_type="application/json", **_hdr(acc)).status_code == 201
    res = client.post("/api/v1/verifications", BODY, content_type="application/json", **_hdr(acc))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "already_pending"
    assert VerificationRequest.objects.filter(account=acc).count() == 1


@pytest.mark.django_db
def test_already_approved_member_gets_409_not_a_phantom(client):
    acc = _owner()
    AccountCapability.objects.create(account=acc, capability="rescuer", status="approved")
    res = client.post("/api/v1/verifications", BODY, content_type="application/json", **_hdr(acc))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "already_verified"
    assert VerificationRequest.objects.filter(account=acc).count() == 0


@pytest.mark.django_db
def test_shelter_cannot_request_rescuer(client):
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    res = client.post("/api/v1/verifications", BODY, content_type="application/json", **_hdr(acc))
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "wrong_account_type"
