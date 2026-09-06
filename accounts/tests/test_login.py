import pytest
from django.utils import timezone

from accounts.factories import AccountFactory


@pytest.mark.django_db
def test_login_verified_returns_tokens(client):
    AccountFactory(email="ok@example.com", password="s3cretpass",
                   email_verified_at=timezone.now())
    res = client.post("/api/v1/auth/login",
                      {"email": "ok@example.com", "password": "s3cretpass"},
                      content_type="application/json")
    assert res.status_code == 200 and res.json()["access"]


@pytest.mark.django_db
def test_login_wrong_password_is_generic_401(client):
    AccountFactory(email="ok2@example.com", password="s3cretpass",
                   email_verified_at=timezone.now())
    res = client.post("/api/v1/auth/login",
                      {"email": "ok2@example.com", "password": "WRONG"},
                      content_type="application/json")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_credentials"


@pytest.mark.django_db
def test_login_unknown_email_is_same_generic_401(client):
    res = client.post("/api/v1/auth/login",
                      {"email": "ghost@example.com", "password": "whatever1"},
                      content_type="application/json")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_credentials"


@pytest.mark.django_db
def test_login_unverified_email_returns_403_and_resends(client):
    from verifications.models import VerificationCode
    acc = AccountFactory(email="unv@example.com", password="s3cretpass")  # unverified
    res = client.post("/api/v1/auth/login",
                      {"email": "unv@example.com", "password": "s3cretpass"},
                      content_type="application/json")
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "email_unverified"
    assert VerificationCode.objects.filter(account=acc, purpose="signup").exists()


def _bearer(acc):
    from accounts.tokens import tokens_for
    return tokens_for(acc)


@pytest.mark.django_db
def test_logout_all_sets_sessions_revoked_at(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    access = _bearer(acc)["access"]
    res = client.post("/api/v1/auth/logout-all", HTTP_AUTHORIZATION=f"Bearer {access}")
    assert res.status_code == 204
    acc.refresh_from_db()
    assert acc.sessions_revoked_at is not None


@pytest.mark.django_db
def test_access_token_issued_before_revocation_is_rejected(client):
    # deterministic: revocation strictly AFTER the token's iat (no same-second fuzz)
    acc = AccountFactory(email_verified_at=timezone.now())
    access = _bearer(acc)["access"]
    acc.sessions_revoked_at = timezone.now() + timezone.timedelta(seconds=30)
    acc.save(update_fields=["sessions_revoked_at"])
    # any IsAuthenticated endpoint now rejects the pre-revocation token
    res = client.post("/api/v1/auth/logout-all", HTTP_AUTHORIZATION=f"Bearer {access}")
    assert res.status_code == 401


@pytest.mark.django_db
def test_refresh_rejected_after_revocation(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    refresh = _bearer(acc)["refresh"]
    acc.sessions_revoked_at = timezone.now() + timezone.timedelta(seconds=30)
    acc.save(update_fields=["sessions_revoked_at"])
    res = client.post("/api/v1/auth/refresh", {"refresh": refresh},
                      content_type="application/json")
    assert res.status_code == 401


@pytest.mark.django_db
def test_token_for_deleted_account_returns_401(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    access = _bearer(acc)["access"]
    acc.delete()
    res = client.post("/api/v1/auth/logout-all", HTTP_AUTHORIZATION=f"Bearer {access}")
    assert res.status_code == 401


@pytest.mark.django_db
def test_refresh_for_deleted_account_returns_401(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    refresh = _bearer(acc)["refresh"]
    acc.delete()
    res = client.post("/api/v1/auth/refresh", {"refresh": refresh},
                      content_type="application/json")
    assert res.status_code == 401


@pytest.mark.django_db
def test_refresh_happy_path_rotates_access_token(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    refresh = _bearer(acc)["refresh"]
    res = client.post("/api/v1/auth/refresh", {"refresh": refresh},
                      content_type="application/json")
    assert res.status_code == 200
    assert res.json()["access"]
