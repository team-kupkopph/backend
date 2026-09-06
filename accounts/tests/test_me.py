import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for


def _auth(client, account):
    token = tokens_for(account)["access"]
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.mark.django_db
def test_me_returns_profile_with_capabilities_and_settings(client):
    acc = AccountFactory(email="me@example.com", email_verified_at=timezone.now())
    res = client.get("/api/v1/me", **_auth(client, acc))
    assert res.status_code == 200
    body = res.json()
    assert body["email"] == "me@example.com"
    assert body["capabilities"] == []
    assert body["shelter"] is None
    assert body["settings"]["marketing_emails"] is False


@pytest.mark.django_db
def test_me_shelter_block_reflects_tier_and_derived_status(client):
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    from shelter.models import ShelterProfile
    from verifications.models import VerificationRequest
    ShelterProfile.objects.create(account=acc, org_name="PAWS", org_type="shelter",
                                  tier="registered_ngo")
    VerificationRequest.objects.create(account=acc, type="shelter_org", status="pending")
    body = client.get("/api/v1/me", **_auth(client, acc)).json()
    assert body["shelter"] == {"tier": "registered_ngo", "verification_status": "pending"}


@pytest.mark.django_db
def test_patch_me_updates_display_name(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.patch("/api/v1/me", {"display_name": "New Name"},
                       content_type="application/json", **_auth(client, acc))
    assert res.status_code == 200
    acc.refresh_from_db()
    assert acc.display_name == "New Name"


@pytest.mark.django_db
def test_patch_me_rejects_overlong_display_name(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.patch("/api/v1/me", {"display_name": "x" * 200},
                       content_type="application/json", **_auth(client, acc))
    assert res.status_code == 400


@pytest.mark.django_db
def test_me_requires_auth(client):
    assert client.get("/api/v1/me").status_code == 401


@pytest.mark.django_db
def test_get_settings_returns_defaults(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.get("/api/v1/me/settings", **_auth(client, acc))
    assert res.status_code == 200
    body = res.json()
    assert body["marketing_emails"] is False
    assert body["approximate_location"] is True


@pytest.mark.django_db
def test_patch_settings_parses_booleans(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.patch("/api/v1/me/settings", {"marketing_emails": True, "push_enabled": False},
                       content_type="application/json", **_auth(client, acc))
    assert res.status_code == 200
    res = client.get("/api/v1/me/settings", **_auth(client, acc))
    body = res.json()
    assert body["marketing_emails"] is True
    assert body["push_enabled"] is False

    # Stringly-typed client sending "false" must not be mis-parsed as truthy
    # (bool("false") is True; a real boolean parser must produce False here).
    res = client.patch("/api/v1/me/settings", {"approximate_location": "false"},
                       content_type="application/json", **_auth(client, acc))
    assert res.status_code == 200
    res = client.get("/api/v1/me/settings", **_auth(client, acc))
    assert res.json()["approximate_location"] is False
