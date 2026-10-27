"""X1 · one session gate. A suspended or deleted account must not get a session by ANY route.

Login always refused them (test_login_suspended, test_account_deletion). Social sign-in and email
verification did not: both minted tokens with no status check, and `AccountJWTAuthentication`
never looked at status either — so a suspension was only as strong as `sessions_revoked_at`, which
a fresh Google sign-in walked straight past.
"""
import re
from pathlib import Path

import pytest
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.factories import AccountFactory
from accounts.models import AccountIdentity, AccountStatus
from common import otp


@pytest.fixture
def fake_verify(monkeypatch):
    def _set(sub, email):
        monkeypatch.setattr("accounts.social.verify_token",
                            lambda provider, id_token: {"sub": sub, "email": email})
    return _set


def _suspended(**kw):
    return AccountFactory(status=AccountStatus.SUSPENDED, email_verified_at=timezone.now(), **kw)


def _deleted(**kw):
    return AccountFactory(status=AccountStatus.DELETED, deleted_at=timezone.now(),
                          email_verified_at=timezone.now(), **kw)


def _social(client):
    return client.post("/api/v1/auth/social/google", {"id_token": "x"},
                       content_type="application/json")


@pytest.mark.django_db
@pytest.mark.parametrize("make", [_suspended, _deleted], ids=["suspended", "deleted"])
def test_social_sign_in_with_a_linked_identity_is_refused(client, fake_verify, make):
    account = make(email="linked@example.com")
    AccountIdentity.objects.create(account=account, provider="google",
                                   provider_user_id="g-1", email=account.email)
    fake_verify("g-1", account.email)

    res = _social(client)

    assert res.status_code == 401
    body = res.json()
    assert "access" not in body and "refresh" not in body
    assert body["error"]["code"] == "account_unavailable"


@pytest.mark.django_db
@pytest.mark.parametrize("make", [_suspended, _deleted], ids=["suspended", "deleted"])
def test_social_sign_in_by_email_is_refused_before_any_link_is_made(client, fake_verify, make):
    # No identity yet: the view would link one by email. It must refuse FIRST — a refused
    # sign-in that still attached a Google account would be a side effect on a locked account.
    account = make(email="bymail@example.com")
    fake_verify("g-2", account.email)

    res = _social(client)

    assert res.status_code == 401
    assert not AccountIdentity.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_email_verify_does_not_mint_tokens_for_a_suspended_account(client):
    account = AccountFactory(email="unverified@example.com", status=AccountStatus.SUSPENDED)
    code = otp.issue_code(account, channel="email", purpose="signup")

    res = client.post("/api/v1/auth/email/verify", {"email": account.email, "code": code},
                      content_type="application/json")

    assert res.status_code == 401
    assert "access" not in res.json()


@pytest.mark.django_db
@pytest.mark.parametrize("make", [_suspended, _deleted], ids=["suspended", "deleted"])
def test_a_token_minted_after_the_revocation_is_still_refused(client, make):
    # Exactly what the social hole produced: a token whose iat is AFTER sessions_revoked_at,
    # so the revocation check alone lets it through. Status has to be checked too.
    account = make()
    account.sessions_revoked_at = timezone.now() - timezone.timedelta(minutes=5)
    account.save(update_fields=["sessions_revoked_at"])
    refresh = RefreshToken()
    refresh["account_id"] = str(account.account_id)

    res = client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    assert res.status_code == 401


@pytest.mark.django_db
def test_refresh_is_refused_for_a_suspended_account(client):
    account = _suspended()
    refresh = RefreshToken()
    refresh["account_id"] = str(account.account_id)

    res = client.post("/api/v1/auth/refresh", {"refresh": str(refresh)},
                      content_type="application/json")

    assert res.status_code == 401
    assert "access" not in res.json()


@pytest.mark.django_db
def test_an_active_account_still_signs_in_through_every_route(client, fake_verify):
    account = AccountFactory(email="ok@example.com", email_verified_at=timezone.now())
    account.set_password("Right1234")
    account.save()

    login = client.post("/api/v1/auth/login", {"email": account.email, "password": "Right1234"},
                        content_type="application/json")
    fake_verify("g-ok", account.email)
    social = _social(client)

    assert login.status_code == 200 and login.json()["access"]
    assert social.status_code == 200 and social.json()["access"]
    refreshed = client.post("/api/v1/auth/refresh", {"refresh": login.json()["refresh"]},
                            content_type="application/json")
    assert refreshed.status_code == 200
    me = client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {social.json()['access']}")
    assert me.status_code == 200


BACKEND = Path(__file__).resolve().parents[2]
IMPORT = re.compile(r"^\s*from accounts\.tokens import .*\btokens_for\b", re.M)


def test_runtime_code_mints_account_tokens_only_through_the_gate():
    """`issue_session` is the gate; `tokens_for` is the raw minting it wraps. Tests mint raw
    tokens freely (they build fixtures), but runtime code must not — a new sign-in route that
    imports `tokens_for` directly would reopen X1 without a single failing test."""
    importers = set()
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in (".venv", "dev") or "tests" in rel.parts:
            continue
        if IMPORT.search(path.read_text(encoding="utf-8")):
            importers.add(rel.as_posix())
    # Prove the scan reaches the tree at all: the gate itself must be found.
    assert "accounts/sessions.py" in importers
    assert importers == {"accounts/sessions.py"}
