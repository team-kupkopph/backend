"""The suite never reaches a provider, whatever the developer's .env says.

WHY THIS EXISTS. `config/settings.py` loads `.env`, and every outbound seam — mail, Sentry,
S3, FCM — is env-sourced and credential-guarded: set the variable, the real backend runs.
That is the right posture for a checkout. It is the wrong one for the test run: with
`EMAIL_PROVIDER=resend` and a key in `.env`, every signup and resend test posted to
api.resend.com (~200ms each, a 422 back), which made `test_otp_resend_throttle_is_identical
_for_a_real_and_an_unknown_email` fail on every local run — and would have mailed real
people had a test used a real address. CI never saw any of it, because CI has no `.env`.

So the suite runs on `config.settings_test`, which is `config.settings` with those seams
blanked before any app's `ready()` runs. These tests hold that in place: the first two would
fail the moment someone points pytest back at `config.settings`, and the last one fails if
a resend ever leaves the process, regardless of how it got there.
"""
import pytest
from django.conf import settings

from accounts.factories import AccountFactory
from common import senders
from common.senders import ConsoleSender, get_sender

OUTBOUND_SEAMS = [
    "EMAIL_PROVIDER", "RESEND_API_KEY", "AWS_SES_REGION",
    "SENTRY_DSN",
    "MEDIA_S3_BUCKET_PUBLIC", "MEDIA_S3_BUCKET_RESTRICTED",
    "FCM_PROJECT_ID", "FCM_CREDENTIALS_PATH",
]


def test_the_suite_runs_on_the_test_settings():
    assert settings.SETTINGS_MODULE == "config.settings_test"


@pytest.mark.parametrize("name", OUTBOUND_SEAMS)
def test_every_outbound_seam_is_blank(name):
    # Reduced to a bool BEFORE the assert, on purpose: pytest prints the operands of a
    # failed comparison, and one of these is an API key. A failure must say which seam
    # leaked, never what leaked (§12.6 applies to CI logs too).
    leaked = bool(getattr(settings, name))
    assert not leaked, f"{name} leaked in from the environment (value withheld)"


def test_the_default_sender_is_the_console_stub():
    assert isinstance(get_sender(), ConsoleSender)


@pytest.mark.django_db
def test_an_otp_resend_never_leaves_the_process(client, monkeypatch):
    """The behavioural half: the exact request that was reaching Resend, with the network
    call replaced by a recorder. A recorder and not a tripwire that raises, because
    `ResendSender.send` catches Exception and falls back to the console — a raise would be
    swallowed and the test would pass against the very configuration it exists to catch."""
    calls = []
    monkeypatch.setattr(senders.requests, "post",
                        lambda *args, **kwargs: calls.append(args[0] if args else kwargs.get("url")))
    AccountFactory(email="real@example.com", email_verified_at=None)
    res = client.post("/api/v1/auth/email/resend", {"email": "real@example.com"},
                      content_type="application/json")
    assert res.status_code == 202
    assert calls == [], "an OTP resend reached requests.post — mail left the process"
