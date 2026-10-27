"""T7 · POST /me/password — change the password while signed in (AS6).

The spec listed this route and no task built it: the only way to change a password was the
forgot-password email-code flow, which is the wrong tool for someone who is already signed in
and knows their password.

What matters beyond the happy path:

  * **Other sessions die, this one lives.** A password change is what you do when you fear
    someone else has the account, so every other session must be revoked (the same
    `sessions_revoked_at` mechanism reset and logout-all use). But the caller must not be signed
    out of the phone they just did it on, so the response carries fresh tokens for THIS device.
  * **Guessing the current password is a login oracle**, so it is throttled per account.
  * **A social-only account has no password to "change"**; it is told to use Forgot password
    to set one, instead of a confusing "incorrect".
"""
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from accounts.factories import AccountFactory
from accounts.models import AccountStatus

URL = "/api/v1/me/password"
CURRENT = "Right1234"
NEW = "Better5678"


@pytest.fixture(autouse=True)
def _clear_throttle_history():
    cache.clear()
    yield
    cache.clear()


def _account(**kw):
    return AccountFactory(email_verified_at=timezone.now(), password=CURRENT, **kw)


def _token(account, *, age=timedelta(0)):
    """An access token for `account`, optionally issued `age` ago. Built directly: a token
    minted in the same second as a revocation is indistinguishable from one minted before it."""
    token = AccessToken()
    token["account_id"] = str(account.account_id)
    token.set_iat(at_time=timezone.now() - age)
    return f"Bearer {token}"


def _change(client, account, current=CURRENT, new=NEW, **kw):
    return client.post(URL, {"current_password": current, "new_password": new},
                       content_type="application/json",
                       HTTP_AUTHORIZATION=_token(account, age=timedelta(minutes=5)), **kw)


@pytest.mark.django_db
def test_changing_the_password_works_and_the_new_one_is_what_signs_in(client):
    account = _account()

    res = _change(client, account)

    assert res.status_code == 200
    account.refresh_from_db()
    assert account.check_password(NEW) and not account.check_password(CURRENT)
    login = client.post("/api/v1/auth/login", {"email": account.email, "password": NEW},
                        content_type="application/json")
    assert login.status_code == 200


@pytest.mark.django_db
def test_every_other_session_dies_but_this_device_stays_signed_in(client):
    account = _account()
    other_device = _token(account, age=timedelta(hours=2))

    res = _change(client, account)
    fresh = res.json()["access"]

    assert client.get("/api/v1/me", HTTP_AUTHORIZATION=other_device).status_code == 401
    assert client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {fresh}").status_code == 200


@pytest.mark.django_db
def test_the_response_carries_a_working_refresh_token_for_this_device(client):
    account = _account()

    res = _change(client, account)

    refreshed = client.post("/api/v1/auth/refresh", {"refresh": res.json()["refresh"]},
                            content_type="application/json")
    assert refreshed.status_code == 200


@pytest.mark.django_db
def test_a_wrong_current_password_is_refused_and_changes_nothing(client):
    account = _account()

    res = _change(client, account, current="Wrong1234")

    assert res.status_code == 400
    err = res.json()["error"]
    assert err["code"] == "invalid_password" and err["field"] == "current_password"
    account.refresh_from_db()
    assert account.check_password(CURRENT)
    assert account.sessions_revoked_at is None            # a failed attempt kills no sessions


@pytest.mark.django_db
def test_the_same_password_again_is_refused(client):
    account = _account()

    res = _change(client, account, new=CURRENT)

    assert res.status_code == 400 and res.json()["error"]["code"] == "password_unchanged"


@pytest.mark.django_db
@pytest.mark.parametrize("weak", ["short1", "nodigitshere", ""])
def test_a_weak_new_password_is_refused_with_the_same_rule_as_signup(client, weak):
    account = _account()

    res = _change(client, account, new=weak)

    assert res.status_code == 400
    account.refresh_from_db()
    assert account.check_password(CURRENT)


@pytest.mark.django_db
def test_a_social_only_account_is_pointed_at_forgot_password(client):
    account = AccountFactory(email_verified_at=timezone.now())
    account.password_hash = ""                       # signed up with Google/Apple
    account.save(update_fields=["password_hash"])

    res = _change(client, account)

    assert res.status_code == 409 and res.json()["error"]["code"] == "no_password"


@pytest.mark.django_db
def test_it_needs_a_signed_in_user(client):
    res = client.post(URL, {"current_password": CURRENT, "new_password": NEW},
                      content_type="application/json")

    assert res.status_code == 401


@pytest.mark.django_db
def test_a_suspended_account_cannot_change_its_password(client):
    account = _account(status=AccountStatus.SUSPENDED)

    assert _change(client, account).status_code == 401


@pytest.mark.django_db
def test_guessing_the_current_password_is_throttled_per_account(client):
    from django.conf import settings
    allowed = int(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["password_change"].split("/")[0])
    account = _account()

    codes = [_change(client, account, current="Wrong1234").status_code for _ in range(allowed + 1)]

    assert codes[:allowed] == [400] * allowed
    assert codes[allowed] == 429


@pytest.mark.django_db
def test_one_accounts_guesses_do_not_throttle_another(client):
    from django.conf import settings
    allowed = int(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["password_change"].split("/")[0])
    guesser, innocent = _account(), _account()
    for _ in range(allowed + 1):
        _change(client, guesser, current="Wrong1234")

    assert _change(client, innocent).status_code == 200
