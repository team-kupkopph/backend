"""T14a · OTP step-up for sensitive actions (AS6, decided 2026-10-27; no authenticator app).

Opt-in per account (`two_factor_enabled`). Login is UNCHANGED: this is step-up, not login 2FA, which
is what Tech Spec §12.1 says. When it is on, the five sensitive routes need a recent one-time-code
proof, sent as `X-Step-Up: <token>`:

    DELETE /me · POST /me/password · POST /me/email · GET /me/export · POST /me/2fa/disable

and nothing else: not login, log out, or settings toggles. A short list on purpose, because a prompt
on every action trains people to ignore it. A social-only account has no password, so for it the
emailed code is the ONLY proof; that is why step-up is code-based and not password-based.

The proof is a signed, account-bound, five-minute token with no table. Turning 2FA on needs a
verified step-up FIRST, which proves the code channel works before the account depends on it: a
typo'd email would otherwise lock the person out of their own export and delete.
"""
from unittest import mock

import pytest
from django.core.cache import cache
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from common.otp import _hash
from verifications.models import VerificationCode

GOOD = "654321"
PASSWORD = "Right1234"
ROUTES = [
    ("delete", "/api/v1/me", {"confirm": True}),
    ("post", "/api/v1/me/password", {"current_password": PASSWORD, "new_password": "Better5678"}),
    ("post", "/api/v1/me/email", {"new_email": "next@example.com", "password": PASSWORD}),
    ("get", "/api/v1/me/export", None),
    ("post", "/api/v1/me/2fa/disable", {}),
]


@pytest.fixture(autouse=True)
def _clear_throttle_history():
    cache.clear()
    yield
    cache.clear()


def _account(*, two_factor=True, **kw):
    kw.setdefault("email", "owner@example.com")
    return AccountFactory(email_verified_at=timezone.now(), password=PASSWORD,
                          two_factor_enabled=two_factor, **kw)


def _hdr(account, step_up=None):
    h = {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}
    if step_up:
        h["HTTP_X_STEP_UP"] = step_up
    return h


def _call(client, method, url, body, **hdr):
    if body is None:
        return getattr(client, method)(url, **hdr)
    return getattr(client, method)(url, body, content_type="application/json", **hdr)


def _send(client, account, **body):
    return client.post("/api/v1/me/step-up", body, content_type="application/json", **_hdr(account))


def _verify(client, account, code=GOOD):
    return client.post("/api/v1/me/step-up/verify", {"code": code}, content_type="application/json",
                       **_hdr(account))


def _know_the_code(account):
    row = VerificationCode.objects.filter(account=account, purpose="stepup",
                                          consumed_at__isnull=True).latest("created_at")
    row.code_hash = _hash(GOOD)
    row.save(update_fields=["code_hash"])


def _proof(client, account):
    """A real step-up token, through the real endpoints."""
    _send(client, account)
    _know_the_code(account)
    res = _verify(client, account)
    assert res.status_code == 200, res.content
    return res.json()["step_up_token"]


# ---------------------------------------------------------------- 2FA off: nothing changes

@pytest.mark.django_db
def test_with_two_factor_off_the_sensitive_routes_need_no_proof(client):
    account = _account(two_factor=False)

    assert _call(client, "get", "/api/v1/me/export", None, **_hdr(account)).status_code == 200
    assert _call(client, "post", "/api/v1/me/password",
                 {"current_password": PASSWORD, "new_password": "Better5678"}, **_hdr(account)).status_code == 200


@pytest.mark.django_db
def test_me_reports_whether_two_step_is_on(client):
    on, off = _account(two_factor=True), _account(two_factor=False, email="off@example.com")

    assert client.get("/api/v1/me", **_hdr(on)).json()["two_factor_enabled"] is True
    assert client.get("/api/v1/me", **_hdr(off)).json()["two_factor_enabled"] is False


# ---------------------------------------------------------------- 2FA on: the five routes

@pytest.mark.django_db
@pytest.mark.parametrize("method,url,body", ROUTES, ids=[r[1] + ":" + r[0] for r in ROUTES])
def test_with_two_factor_on_every_sensitive_route_wants_a_proof(client, method, url, body):
    account = _account()

    res = _call(client, method, url, body, **_hdr(account))

    assert res.status_code == 403
    assert res.json()["error"]["code"] == "step_up_required"


@pytest.mark.django_db
@pytest.mark.parametrize("method,url,body", ROUTES, ids=[r[1] + ":" + r[0] for r in ROUTES])
def test_a_proof_lets_each_of_them_through(client, method, url, body):
    account = _account()
    token = _proof(client, account)

    res = _call(client, method, url, body, **_hdr(account, step_up=token))

    assert res.status_code != 403, res.content


@pytest.mark.django_db
def test_login_logout_and_settings_are_never_asked_for_a_proof(client):
    account = _account()

    assert client.get("/api/v1/me", **_hdr(account)).status_code == 200
    assert client.get("/api/v1/me/settings", **_hdr(account)).status_code == 200
    assert client.patch("/api/v1/me/settings", {"marketing_emails": True}, content_type="application/json",
                        **_hdr(account)).status_code == 200
    assert client.post("/api/v1/auth/login", {"email": "owner@example.com", "password": PASSWORD},
                       content_type="application/json").status_code == 200


# ---------------------------------------------------------------- getting a proof

@pytest.mark.django_db
def test_the_code_goes_to_the_verified_email_and_the_reply_shows_only_a_masked_address(client):
    account = _account()

    with mock.patch("common.otp.get_sender") as get_sender:
        res = _send(client, account)

    assert res.status_code == 202
    kwargs = get_sender.return_value.send.call_args.kwargs
    assert kwargs["channel"] == "email" and kwargs["to"] == "owner@example.com"
    assert kwargs["purpose"] == "stepup"
    assert res.json() == {"sent_to": "o***@example.com"}
    assert "owner@example.com" not in res.content.decode()


@pytest.mark.django_db
def test_the_code_can_go_by_sms_to_a_verified_phone(client):
    account = _account(phone="+639171110000", phone_verified_at=timezone.now())

    with mock.patch("common.otp.get_sender") as get_sender:
        res = _send(client, account, channel="sms")

    assert res.status_code == 202
    assert get_sender.return_value.send.call_args.kwargs["channel"] == "sms"
    assert "+639171110000" not in res.content.decode()
    assert res.json()["sent_to"].endswith("0000") and "***" in res.json()["sent_to"]


@pytest.mark.django_db
def test_sms_without_a_verified_phone_is_refused_not_silently_emailed(client):
    account = _account(phone="+639171110000", phone_verified_at=None)

    res = _send(client, account, channel="sms")

    assert res.status_code == 409 and res.json()["error"]["code"] == "no_verified_channel"


@pytest.mark.django_db
def test_an_unverified_email_is_not_a_channel_to_prove_anything_over(client):
    account = AccountFactory(email="owner@example.com", password=PASSWORD, email_verified_at=None)

    res = _send(client, account)

    assert res.status_code == 409 and res.json()["error"]["code"] == "no_verified_channel"


@pytest.mark.django_db
def test_requests_for_a_code_are_throttled(client):
    account = _account()

    statuses = [_send(client, account).status_code for _ in range(8)]

    assert statuses[0] == 202 and 429 in statuses


@pytest.mark.django_db
def test_a_wrong_code_costs_an_attempt_and_five_lock_it(client):
    account = _account()
    _send(client, account)

    first = _verify(client, account, code="000000")
    for _ in range(4):
        _verify(client, account, code="000000")

    assert first.status_code == 400 and first.json()["error"]["details"]["attempts_left"] == 4
    assert _verify(client, account, code="000000").status_code == 423


@pytest.mark.django_db
def test_verifying_returns_a_five_minute_token(client):
    account = _account()
    _send(client, account)
    _know_the_code(account)

    res = _verify(client, account)

    assert res.status_code == 200
    assert res.json()["expires_in"] == 300 and res.json()["step_up_token"]


@pytest.mark.django_db
def test_signing_in_is_required(client):
    assert client.post("/api/v1/me/step-up", {}, content_type="application/json").status_code == 401
    assert client.post("/api/v1/me/step-up/verify", {"code": GOOD},
                       content_type="application/json").status_code == 401


# ---------------------------------------------------------------- what a token is worth

@pytest.mark.django_db
def test_a_token_is_good_for_several_actions_inside_its_window(client):
    account = _account()
    token = _proof(client, account)

    first = _call(client, "get", "/api/v1/me/export", None, **_hdr(account, step_up=token))
    second = _call(client, "get", "/api/v1/me/export", None, **_hdr(account, step_up=token))

    assert first.status_code == second.status_code == 200


@pytest.mark.django_db
def test_a_token_dies_after_five_minutes(client):
    account = _account()
    token = _proof(client, account)

    with mock.patch("django.core.signing.time.time", return_value=__import__("time").time() + 301):
        res = _call(client, "get", "/api/v1/me/export", None, **_hdr(account, step_up=token))

    assert res.status_code == 403 and res.json()["error"]["code"] == "step_up_required"


@pytest.mark.django_db
def test_a_token_is_bound_to_the_account_that_earned_it(client):
    a, b = _account(), _account(email="b@example.com")
    token = _proof(client, a)

    res = _call(client, "get", "/api/v1/me/export", None, **_hdr(b, step_up=token))

    assert res.status_code == 403 and res.json()["error"]["code"] == "step_up_required"


@pytest.mark.django_db
def test_junk_in_the_header_is_just_no_proof(client):
    account = _account()

    for junk in ("", "garbage", "a:b:c", "x" * 500):
        res = _call(client, "get", "/api/v1/me/export", None, **_hdr(account, step_up=junk or None))
        assert res.status_code == 403 and res.json()["error"]["code"] == "step_up_required"


# ---------------------------------------------------------------- turning it on and off

@pytest.mark.django_db
def test_turning_it_on_needs_a_verified_proof_first(client):
    account = _account(two_factor=False)

    refused = client.post("/api/v1/me/2fa/enable", {}, content_type="application/json", **_hdr(account))
    token = _proof(client, account)
    enabled = client.post("/api/v1/me/2fa/enable", {}, content_type="application/json",
                          **_hdr(account, step_up=token))

    assert refused.status_code == 403 and refused.json()["error"]["code"] == "step_up_required"
    assert enabled.status_code == 200 and enabled.json() == {"two_factor_enabled": True}
    account.refresh_from_db()
    assert account.two_factor_enabled is True
    assert client.get("/api/v1/me", **_hdr(account)).json()["two_factor_enabled"] is True


@pytest.mark.django_db
def test_turning_it_off_needs_a_proof_too(client):
    account = _account(two_factor=True)

    refused = client.post("/api/v1/me/2fa/disable", {}, content_type="application/json", **_hdr(account))
    token = _proof(client, account)
    disabled = client.post("/api/v1/me/2fa/disable", {}, content_type="application/json",
                           **_hdr(account, step_up=token))

    assert refused.status_code == 403
    assert disabled.status_code == 200 and disabled.json() == {"two_factor_enabled": False}
    account.refresh_from_db()
    assert account.two_factor_enabled is False


@pytest.mark.django_db
def test_turning_it_on_twice_or_off_twice_is_harmless(client):
    on, off = _account(two_factor=True), _account(two_factor=False, email="off@example.com")

    again_on = client.post("/api/v1/me/2fa/enable", {}, content_type="application/json",
                           **_hdr(on, step_up=_proof(client, on)))
    again_off = client.post("/api/v1/me/2fa/disable", {}, content_type="application/json",
                            **_hdr(off, step_up=_proof(client, off)))

    assert again_on.json() == {"two_factor_enabled": True}
    assert again_off.json() == {"two_factor_enabled": False}
