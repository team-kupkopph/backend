"""F9 (test-plan, 2026-09-14) · `social_proof_url` must be an http(s) URL when present.

dev/onboarding-validation.md: the social link "parses as an http(s) URL". The field was a bare
CharField, so `javascript:alert(1)` was stored and rendered to reviewers in the admin console.
"""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from shelter.models import ShelterProfile


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _post_rescuer(client, url):
    acc = AccountFactory(email_verified_at=timezone.now())
    return client.post("/api/v1/verifications", {
        "type": "rescuer", "social_proof_url": url, "consent_version": "2026-08-01",
        "documents": [{"doc_type": "gov_id", "file_url": "https://x/gov.jpg"}],
    }, content_type="application/json", **_hdr(acc))


SHELTER_DOCS = ([{"doc_type": "gov_id", "file_url": "https://x/gov.jpg"},
                 {"doc_type": "proof_billing", "file_url": "https://x/bill.jpg"}]
                + [{"doc_type": "rescue_photos", "file_url": f"https://x/p{i}.jpg"} for i in range(3)])


def _post_shelter(client, url):
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    ShelterProfile.objects.create(account=acc, org_name="PAWS", org_type="shelter",
                                  tier="community_rescue")
    return client.post("/api/v1/verifications", {
        "type": "shelter_org", "social_proof_url": url, "consent_version": "2026-08-01",
        "documents": SHELTER_DOCS,
    }, content_type="application/json", **_hdr(acc))


@pytest.mark.django_db
@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://ana.rescues/x", "ana.rescues"])
def test_rescuer_non_http_social_proof_url_is_400_naming_the_field(client, bad):
    res = _post_rescuer(client, bad)
    assert res.status_code == 400
    assert res.json()["error"]["field"] == "social_proof_url"


@pytest.mark.django_db
@pytest.mark.parametrize("good", ["https://facebook.com/x", "https://linktr.ee/x", "http://ana.rescues/x"])
def test_rescuer_http_social_proof_url_is_accepted_and_stored(client, good):
    res = _post_rescuer(client, good)
    assert res.status_code == 201
    from verifications.models import VerificationRequest
    assert VerificationRequest.objects.get(type="rescuer").social_proof_url == good


@pytest.mark.django_db
def test_rescuer_blank_social_proof_url_is_still_accepted(client):
    # The field is optional at the API; the mobile screens enforce "required" for the member arm.
    res = _post_rescuer(client, "")
    assert res.status_code == 201


@pytest.mark.django_db
def test_rescuer_omitted_social_proof_url_is_still_accepted(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.post("/api/v1/verifications", {
        "type": "rescuer", "consent_version": "2026-08-01",
        "documents": [{"doc_type": "gov_id", "file_url": "https://x/gov.jpg"}],
    }, content_type="application/json", **_hdr(acc))
    assert res.status_code == 201


@pytest.mark.django_db
def test_shelter_non_http_social_proof_url_is_400_naming_the_field(client):
    res = _post_shelter(client, "javascript:alert(1)")
    assert res.status_code == 400
    assert res.json()["error"]["field"] == "social_proof_url"


@pytest.mark.django_db
def test_shelter_http_social_proof_url_is_accepted(client):
    res = _post_shelter(client, "https://facebook.com/pawsph")
    assert res.status_code == 201
