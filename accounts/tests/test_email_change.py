"""T13 · changing the sign-in email (AS6, decided 2026-10-27).

The email is the account's recovery channel: whoever controls it can reset the password. So
changing it is the most valuable thing a hijacked session could do, and every rule here exists
to make that hard while keeping the honest case (a new address, a typo, a lost inbox) easy:

  * **The new address must be PROVEN** before it is written. The code goes to the NEW address and
    is kept with the code row (`verification_code.target`, like the phone change); the account
    keeps its current email until the code comes back.
  * **The current password is required**, so a stolen access token alone cannot start it. A
    social-only account has no password and is told to set one first (same as change-password).
  * **Every other session is revoked** on success, and this device gets fresh tokens.
  * **The OLD address is told**, so a change made from a hijacked session cannot be silent.
  * **An address already in use is not an oracle.** The request answers exactly as it does for a
    free address and creates the same code row, so neither the response nor the attempt counter
    reveals who has an account. No mail is sent to it.
"""
from datetime import timedelta
from unittest import mock

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from accounts.factories import AccountFactory
from accounts.models import AccountIdentity
from common.otp import _hash
from verifications.models import VerificationCode

PASSWORD = "Right1234"
OLD = "old.address@example.com"
NEW = "new.address@example.com"
GOOD = "654321"


@pytest.fixture(autouse=True)
def _clear_throttle_history():
    cache.clear()
    yield
    cache.clear()


def _token(account, *, age=timedelta(minutes=5)):
    token = AccessToken()
    token["account_id"] = str(account.account_id)
    token.set_iat(at_time=timezone.now() - age)
    return f"Bearer {token}"


def _hdr(account):
    return {"HTTP_AUTHORIZATION": _token(account)}


def _account(**kw):
    kw.setdefault("email", OLD)
    return AccountFactory(email_verified_at=timezone.now(), password=PASSWORD, **kw)


def _request(client, account, new=NEW, password=PASSWORD):
    return client.post("/api/v1/me/email", {"new_email": new, "password": password},
                       content_type="application/json", **_hdr(account))


def _verify(client, account, code=GOOD):
    return client.post("/api/v1/me/email/verify", {"code": code}, content_type="application/json",
                       **_hdr(account))


def _know_the_code(account):
    row = VerificationCode.objects.filter(account=account, purpose="email",
                                          consumed_at__isnull=True).latest("created_at")
    row.code_hash = _hash(GOOD)
    row.save(update_fields=["code_hash"])
    return row


# ------------------------------------------------------------------ requesting the change

@pytest.mark.django_db
def test_the_code_goes_to_the_new_address_and_the_account_keeps_its_email(client):
    account = _account()

    with mock.patch("common.otp.get_sender") as get_sender:
        res = _request(client, account)

    assert res.status_code == 202
    kwargs = get_sender.return_value.send.call_args.kwargs
    assert kwargs["channel"] == "email" and kwargs["to"] == NEW and kwargs["purpose"] == "email"
    account.refresh_from_db()
    assert account.email == OLD                      # nothing changes until the code comes back
    row = VerificationCode.objects.get(account=account, purpose="email")
    assert row.target == NEW


@pytest.mark.django_db
def test_the_current_password_is_required(client):
    account = _account()

    with mock.patch("common.otp.get_sender") as get_sender:
        wrong = _request(client, account, password="Wrong1234")
        missing = client.post("/api/v1/me/email", {"new_email": NEW}, content_type="application/json",
                              **_hdr(account))

    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "invalid_password"
    assert missing.status_code == 400
    get_sender.return_value.send.assert_not_called()
    assert not VerificationCode.objects.filter(account=account, purpose="email").exists()


@pytest.mark.django_db
def test_a_social_only_account_must_set_a_password_first(client):
    account = _account()
    account.password_hash = ""                       # signed up with Google/Apple
    account.save(update_fields=["password_hash"])

    res = _request(client, account, password="anything")

    assert res.status_code == 409 and res.json()["error"]["code"] == "no_password"


@pytest.mark.django_db
def test_a_malformed_or_unchanged_address_is_refused(client):
    account = _account()

    assert _request(client, account, new="not-an-email").status_code == 400
    same = _request(client, account, new=OLD.upper())
    assert same.status_code == 400 and same.json()["error"]["code"] == "email_unchanged"


@pytest.mark.django_db
def test_an_address_in_use_is_not_an_oracle(client):
    """Same status, same body, a code row with the same attempts budget, and NO mail to the
    address, whether or not somebody already owns it."""
    AccountFactory(email="taken@example.com")
    free_account, taken_account = _account(email="a@example.com"), _account(email="b@example.com")

    with mock.patch("common.otp.get_sender") as free_sender:
        free = _request(client, free_account, new="nobody@example.com")
    with mock.patch("common.otp.get_sender") as taken_sender:
        taken = _request(client, taken_account, new="TAKEN@example.com")   # also case-insensitive

    assert (free.status_code, free.json()) == (taken.status_code, taken.json()) == (202, {})
    free_sender.return_value.send.assert_called_once()
    taken_sender.return_value.send.assert_not_called()                      # no mail to the owner
    # A wrong guess at either costs the same and reports the same budget.
    guess_free, guess_taken = _verify(client, free_account), _verify(client, taken_account)
    assert guess_free.status_code == guess_taken.status_code == 400
    strip = lambda body: {**body, "error": {k: v for k, v in body["error"].items() if k != "request_id"}}  # noqa: E731
    assert strip(guess_free.json()) == strip(guess_taken.json())


@pytest.mark.django_db
def test_one_pending_change_per_account(client):
    account = _account()
    _request(client, account, new="first@example.com")
    first = _know_the_code(account)

    _request(client, account, new="second@example.com")

    assert VerificationCode.objects.filter(account=account, purpose="email",
                                           consumed_at__isnull=True).count() == 1
    assert not VerificationCode.objects.filter(pk=first.pk).exists()
    assert VerificationCode.objects.get(account=account, purpose="email").target == "second@example.com"


@pytest.mark.django_db
def test_requests_are_throttled_per_account(client):
    account = _account()

    statuses = [_request(client, account, new=f"n{i}@example.com").status_code for i in range(7)]

    assert statuses[:5] == [202] * 5 and 429 in statuses[5:]


@pytest.mark.django_db
def test_signing_in_is_required(client):
    assert client.post("/api/v1/me/email", {"new_email": NEW, "password": "x"},
                       content_type="application/json").status_code == 401
    assert client.post("/api/v1/me/email/verify", {"code": GOOD},
                       content_type="application/json").status_code == 401


# ------------------------------------------------------------------ confirming it

@pytest.mark.django_db
def test_the_right_code_swaps_the_email_and_the_new_one_signs_in(client):
    account = _account()
    _request(client, account)
    _know_the_code(account)

    res = _verify(client, account)

    assert res.status_code == 200, res.content
    body = res.json()
    assert body["email"] == NEW and body["access"] and body["refresh"]
    account.refresh_from_db()
    assert account.email == NEW and account.email_verified_at is not None
    login = client.post("/api/v1/auth/login", {"email": NEW, "password": PASSWORD},
                        content_type="application/json")
    assert login.status_code == 200
    assert client.post("/api/v1/auth/login", {"email": OLD, "password": PASSWORD},
                       content_type="application/json").status_code == 401


@pytest.mark.django_db
def test_every_other_session_dies_and_this_device_keeps_going(client):
    account = _account()
    _request(client, account)
    _know_the_code(account)

    res = _verify(client, account)

    stale = client.get("/api/v1/me", HTTP_AUTHORIZATION=_token(account, age=timedelta(minutes=10)))
    fresh = client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {res.json()['access']}")
    assert stale.status_code == 401
    assert fresh.status_code == 200 and fresh.json()["email"] == NEW


@pytest.mark.django_db
def test_the_old_address_is_told_and_the_new_one_is_not_sent_a_notice(client):
    account = _account()
    _request(client, account)
    _know_the_code(account)

    with mock.patch("accounts.notices.get_sender") as get_sender:
        _verify(client, account)

    get_sender.return_value.send_notice.assert_called_once()
    kwargs = get_sender.return_value.send_notice.call_args.kwargs
    assert kwargs["to"] == OLD
    assert NEW not in kwargs["body"]                 # the notice masks the new address
    assert "n***@" in kwargs["body"]


@pytest.mark.django_db
def test_a_notice_that_cannot_be_sent_does_not_undo_the_change(client):
    account = _account()
    _request(client, account)
    _know_the_code(account)

    with mock.patch("accounts.notices.get_sender", side_effect=RuntimeError("mail is down")):
        res = _verify(client, account)

    assert res.status_code == 200
    account.refresh_from_db()
    assert account.email == NEW


@pytest.mark.django_db
def test_a_wrong_code_costs_an_attempt_and_five_lock_it(client):
    account = _account()
    _request(client, account)

    first = _verify(client, account, code="000000")
    for _ in range(4):
        last = _verify(client, account, code="000000")

    assert first.status_code == 400 and first.json()["error"]["details"]["attempts_left"] == 4
    assert last.status_code in (400, 423)
    assert _verify(client, account, code="000000").status_code == 423
    account.refresh_from_db()
    assert account.email == OLD


@pytest.mark.django_db
def test_an_address_taken_in_the_meantime_is_refused_and_the_code_is_spent(client):
    account = _account()
    _request(client, account)
    _know_the_code(account)
    AccountFactory(email=NEW)                        # somebody else signs up with it meanwhile

    res = _verify(client, account)

    assert res.status_code == 409 and res.json()["error"]["code"] == "email_taken"
    account.refresh_from_db()
    assert account.email == OLD
    assert _verify(client, account).status_code == 400          # spent: start over


@pytest.mark.django_db
def test_verifying_with_nothing_pending_changes_nothing(client):
    account = _account()

    res = _verify(client, account)

    assert res.status_code == 400
    account.refresh_from_db()
    assert account.email == OLD


@pytest.mark.django_db
def test_a_linked_provider_address_is_left_alone(client):
    account = _account()
    AccountIdentity.objects.create(account=account, provider="google", provider_user_id="sub-1",
                                   email="gmail.address@gmail.com")
    _request(client, account)
    _know_the_code(account)

    _verify(client, account)

    assert AccountIdentity.objects.get(account=account).email == "gmail.address@gmail.com"
