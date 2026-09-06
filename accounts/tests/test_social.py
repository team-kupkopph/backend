import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Account, AccountIdentity


@pytest.fixture
def fake_verify(monkeypatch):
    def _set(sub, email):
        monkeypatch.setattr("accounts.social.verify_token",
                            lambda provider, id_token: {"sub": sub, "email": email})
    return _set


@pytest.mark.django_db
def test_social_creates_verified_account_when_new(client, fake_verify):
    fake_verify("g-123", "new@example.com")
    res = client.post("/api/v1/auth/social/google",
                      {"id_token": "x", "account_type": "personal"},
                      content_type="application/json")
    assert res.status_code == 200 and res.json()["is_new"] is True
    acc = Account.objects.get(email="new@example.com")
    assert acc.email_verified_at is not None
    assert AccountIdentity.objects.filter(provider="google", provider_user_id="g-123").exists()


@pytest.mark.django_db
def test_social_links_to_existing_account_by_email(client, fake_verify):
    account = AccountFactory(email="known@example.com", email_verified_at=timezone.now())
    fake_verify("g-999", "known@example.com")
    res = client.post("/api/v1/auth/social/google", {"id_token": "x"},
                      content_type="application/json")
    assert res.status_code == 200 and res.json()["is_new"] is False
    assert Account.objects.filter(email="known@example.com").count() == 1
    assert AccountIdentity.objects.filter(provider="google", provider_user_id="g-999",
                                          account=account).exists()


@pytest.mark.django_db
def test_social_existing_identity_logs_in_same_account(client, fake_verify):
    fake_verify("g-777", "repeat@example.com")
    res1 = client.post("/api/v1/auth/social/google", {"id_token": "x"},
                       content_type="application/json")
    assert res1.status_code == 200 and res1.json()["is_new"] is True

    res2 = client.post("/api/v1/auth/social/google", {"id_token": "x"},
                       content_type="application/json")
    assert res2.status_code == 200 and res2.json()["is_new"] is False

    assert Account.objects.count() == 1
    assert AccountIdentity.objects.count() == 1


@pytest.mark.django_db
def test_social_missing_email_returns_400(client, fake_verify):
    fake_verify("g-x", "")
    res = client.post("/api/v1/auth/social/google", {"id_token": "x"},
                      content_type="application/json")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "email_required"


@pytest.mark.django_db
def test_social_admin_account_type_is_ignored(client, fake_verify):
    fake_verify("g-admin", "wannabe-admin@example.com")
    res = client.post("/api/v1/auth/social/google",
                      {"id_token": "x", "account_type": "admin"},
                      content_type="application/json")
    assert res.status_code == 200
    acc = Account.objects.get(email="wannabe-admin@example.com")
    assert acc.account_type == "personal"


@pytest.mark.django_db
def test_unwired_provider_returns_clean_503_not_500(client):
    """The verification seam is deliberately unimplemented (blocked on S0-05/S0-06). It must
    fail as a typed 503 the client can explain, never as an opaque 500."""
    res = client.post("/api/v1/auth/social/google", {"id_token": "x"},
                      content_type="application/json")
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "social_not_configured"


@pytest.mark.django_db
def test_unsupported_provider_is_rejected(client):
    res = client.post("/api/v1/auth/social/tiktok", {"id_token": "x"},
                      content_type="application/json")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "unsupported_provider"
