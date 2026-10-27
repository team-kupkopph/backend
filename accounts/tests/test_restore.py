"""AS1 · restoring a deleted account during the 30-day grace window.

The delete screen promises "30 days to change your mind". Until now nothing delivered it: login
treated a deleted account exactly like a wrong password, so the person could never get back in.

Two properties matter more than the happy path:

  * **No enumeration oracle.** A WRONG password on a deleted account stays byte-identical to an
    unknown address (§12.1). Only a CORRECT password, or a verified Google/Apple identity — proof
    the caller owns the account — is told it is deleted and offered a restore.
  * **Restore does not resurrect old sessions.** `sessions_revoked_at` is left alone, so every
    token issued before the deletion stays dead; the restore mints a fresh session.
"""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from accounts.factories import AccountFactory
from accounts.models import AccountIdentity, AccountStatus
from accounts.purge import anonymize_expired_accounts
from verifications.models import VerificationCode

PASSWORD = "Right1234"
LOGIN = "/api/v1/auth/login"
RESTORE = "/api/v1/auth/restore"


def _deleted(days_ago=2, **kw):
    """A deleted account inside its grace window. Backdated so a session minted by the restore
    has an `iat` safely AFTER `sessions_revoked_at` (the check is second-granular)."""
    then = timezone.now() - timedelta(days=days_ago)
    return AccountFactory(status=AccountStatus.DELETED, deleted_at=then, sessions_revoked_at=then,
                          email_verified_at=then, password=PASSWORD, **kw)


def _post(client, url, body, **kw):
    return client.post(url, body, content_type="application/json", **kw)


def _challenge(client, account):
    res = _post(client, LOGIN, {"email": account.email, "password": PASSWORD})
    assert res.status_code == 409, res.content
    return res.json()["error"]["details"]["restore_token"]


@pytest.fixture
def fake_verify(monkeypatch):
    def _set(sub, email):
        monkeypatch.setattr("accounts.social.verify_token",
                            lambda provider, id_token: {"sub": sub, "email": email})
    return _set


# ----------------------------------------------------------------- the challenge

@pytest.mark.django_db
def test_a_wrong_password_on_a_deleted_account_is_still_the_generic_401(client):
    account = _deleted()
    headers = {"HTTP_X_REQUEST_ID": "fixed-id"}   # pin the tracing id so bodies compare equal

    wrong = _post(client, LOGIN, {"email": account.email, "password": "Wrong1234"}, **headers)
    unknown = _post(client, LOGIN, {"email": "nobody@example.com", "password": "Wrong1234"},
                    **headers)

    assert wrong.status_code == 401
    assert wrong.json() == unknown.json()           # §12.1: no "deleted" oracle without proof


@pytest.mark.django_db
def test_the_correct_password_on_a_deleted_account_offers_a_restore(client):
    account = _deleted(days_ago=2)

    res = _post(client, LOGIN, {"email": account.email, "password": PASSWORD})

    assert res.status_code == 409
    body = res.json()
    assert "access" not in body and "refresh" not in body
    err = body["error"]
    assert err["code"] == "account_deleted"
    assert err["details"]["restore_token"]
    expected = timezone.localtime(account.deleted_at + timedelta(days=30)).date().isoformat()
    assert err["details"]["purge_on"] == expected


@pytest.mark.django_db
def test_a_suspended_account_with_the_right_password_gets_no_offer(client):
    account = AccountFactory(status=AccountStatus.SUSPENDED, email_verified_at=timezone.now(),
                             password=PASSWORD)

    res = _post(client, LOGIN, {"email": account.email, "password": PASSWORD})

    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_credentials"


@pytest.mark.django_db
def test_an_anonymised_account_cannot_be_restored(client):
    account = _deleted(days_ago=40)
    anonymize_expired_accounts()
    account.refresh_from_db()
    assert account.anonymized_at is not None

    # The purge blanks the password, so login cannot even reach the offer.
    res = _post(client, LOGIN, {"email": account.email, "password": PASSWORD})
    assert res.status_code == 401


@pytest.mark.django_db
def test_social_sign_in_on_a_deleted_account_offers_a_restore_without_linking(
        client, fake_verify):
    account = _deleted(email="soc@example.com")
    fake_verify("g-del", account.email)

    res = _post(client, "/api/v1/auth/social/google", {"id_token": "x"})

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "account_deleted"
    assert res.json()["error"]["details"]["restore_token"]
    # A sign-in that did not complete must not leave a provider attached to the account.
    assert not AccountIdentity.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_social_sign_in_through_a_linked_identity_offers_a_restore(client, fake_verify):
    account = _deleted(email="linked@example.com")
    AccountIdentity.objects.create(account=account, provider="google",
                                   provider_user_id="g-linked", email=account.email)
    fake_verify("g-linked", account.email)

    res = _post(client, "/api/v1/auth/social/google", {"id_token": "x"})

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "account_deleted"


# ----------------------------------------------------------------------- restoring

@pytest.mark.django_db
def test_restoring_reactivates_the_account_and_signs_in(client):
    account = _deleted()
    token = _challenge(client, account)

    res = _post(client, RESTORE, {"restore_token": token})

    assert res.status_code == 200
    body = res.json()
    assert body["access"] and body["refresh"]
    assert body["account"]["email"] == account.email
    account.refresh_from_db()
    assert account.status == AccountStatus.ACTIVE
    assert account.deleted_at is None          # the M5 CHECK requires the pair to move together
    me = client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {body['access']}")
    assert me.status_code == 200


@pytest.mark.django_db
def test_restoring_does_not_revive_a_token_issued_before_the_deletion(client):
    account = _deleted()
    # An ACCESS token built directly: `RefreshToken().access_token` stamps iat = now, which
    # would make the "old" token brand new and the assertion below meaningless.
    stale = AccessToken()
    stale["account_id"] = str(account.account_id)
    stale.set_iat(at_time=account.deleted_at - timedelta(hours=1))
    token = _challenge(client, account)
    assert _post(client, RESTORE, {"restore_token": token}).status_code == 200

    old = client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {stale}")

    assert old.status_code == 401


@pytest.mark.django_db
def test_a_restore_token_works_once(client):
    account = _deleted()
    token = _challenge(client, account)

    first = _post(client, RESTORE, {"restore_token": token})
    second = _post(client, RESTORE, {"restore_token": token})

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "restore_token_invalid"


@pytest.mark.django_db
def test_a_newer_challenge_replaces_the_older_one(client):
    account = _deleted()
    older = _challenge(client, account)
    newer = _challenge(client, account)

    assert _post(client, RESTORE, {"restore_token": older}).status_code == 400
    assert _post(client, RESTORE, {"restore_token": newer}).status_code == 200


@pytest.mark.django_db
def test_an_expired_token_is_refused_with_the_same_body_as_a_garbage_one(client):
    account = _deleted()
    token = _challenge(client, account)
    VerificationCode.objects.filter(account=account, purpose="restore").update(
        expires_at=timezone.now() - timedelta(seconds=1))
    headers = {"HTTP_X_REQUEST_ID": "fixed-id"}

    expired = _post(client, RESTORE, {"restore_token": token}, **headers)
    garbage = _post(client, RESTORE, {"restore_token": "not-a-token"}, **headers)

    assert expired.status_code == 400
    assert expired.json() == garbage.json()         # no oracle for "that was once valid"
    account.refresh_from_db()
    assert account.status == AccountStatus.DELETED


@pytest.mark.django_db
@pytest.mark.parametrize("body", [{}, {"restore_token": ""}, {"restore_token": 123},
                                  {"restore_token": ["a"]}])
def test_a_missing_or_malformed_token_is_a_400_not_a_500(client, body):
    res = _post(client, RESTORE, body)

    assert res.status_code == 400
    assert res.json()["error"]["code"] == "restore_token_invalid"


@pytest.mark.django_db
def test_a_token_is_dead_once_the_account_has_been_anonymised(client):
    account = _deleted(days_ago=40)
    token = _challenge(client, account)       # the offer exists until the sweep actually runs
    anonymize_expired_accounts()

    res = _post(client, RESTORE, {"restore_token": token})

    assert res.status_code == 400
    account.refresh_from_db()
    assert account.status == AccountStatus.DELETED and account.anonymized_at is not None


@pytest.mark.django_db
def test_a_token_is_dead_if_the_account_was_suspended_in_the_meantime(client):
    account = _deleted()
    token = _challenge(client, account)
    # Not reachable through the console (it refuses to suspend a deleted account), but the
    # token must not be the thing that lifts a status it did not set.
    type(account).objects.filter(pk=account.pk).update(status=AccountStatus.SUSPENDED,
                                                       deleted_at=None)

    res = _post(client, RESTORE, {"restore_token": token})

    assert res.status_code == 400
    account.refresh_from_db()
    assert account.status == AccountStatus.SUSPENDED


@pytest.mark.django_db
def test_a_restored_account_is_not_swept_by_the_purge(client):
    account = _deleted(days_ago=40)           # old enough that it WOULD be purged
    token = _challenge(client, account)
    assert _post(client, RESTORE, {"restore_token": token}).status_code == 200

    touched = anonymize_expired_accounts()

    assert touched == []
    account.refresh_from_db()
    assert account.anonymized_at is None and account.status == AccountStatus.ACTIVE


@pytest.mark.django_db
def test_the_token_is_stored_hashed_never_in_the_clear(client):
    account = _deleted()
    token = _challenge(client, account)

    row = VerificationCode.objects.get(account=account, purpose="restore")

    assert token not in row.code_hash and row.code_hash != token
    assert row.expires_at <= timezone.now() + timedelta(minutes=10, seconds=5)
