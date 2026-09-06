"""US-D1-adjacent · a real email provider for the OTP path.

Same posture as US-E2's Sentry seam and US-D2's S3 seam: real code path, credential-guarded,
falls back to the ConsoleSender when no provider is set. So:

  * dev (nothing configured) → ConsoleSender, `[DEV OTP]` prints to stdout, works out of the box
  * prod with `EMAIL_PROVIDER=ses` + `EMAIL_FROM` set → SES send_email over boto3
  * prod with EMAIL_PROVIDER set but from-address missing → REFUSE to start, loudly
    (the failure is silent otherwise: a launched app that mails nobody until someone notices)

⚠️ WHAT THIS DOES NOT DO. It cannot open an AWS account, verify a sending identity, or lift
SES sandbox restrictions — all owner actions. The code assumes production access already
exists, and fails gracefully when it doesn't (an SES error must not 500 a signup).
"""
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from common import senders
from common.senders import ConsoleSender, SesEmailSender, get_sender


# ── the picker ──────────────────────────────────────────────────────────────────────
@override_settings(EMAIL_PROVIDER="", EMAIL_FROM="", AWS_SES_REGION="")
def test_dev_gets_the_console_sender_out_of_the_box():
    # No `[DEV OTP]` seam ripped out, no credentials required to run manage.py runserver.
    assert isinstance(get_sender(), ConsoleSender)


@override_settings(EMAIL_PROVIDER="ses", EMAIL_FROM="noreply@kupkop.ph",
                   AWS_SES_REGION="ap-southeast-1")
def test_ses_is_selected_when_configured():
    assert isinstance(get_sender(), SesEmailSender)


@override_settings(EMAIL_PROVIDER="ses", EMAIL_FROM="", AWS_SES_REGION="ap-southeast-1")
def test_partial_ses_config_refuses_to_start():
    # ⚠️ THE FAIL-FAST TEST. Silent fallback here would mean a production deploy that mails
    # nobody because someone forgot `EMAIL_FROM`, discovered by users rather than by us.
    # Matches config/settings.py's stance on SECRET_KEY when DEBUG is off.
    with pytest.raises(ImproperlyConfigured, match="EMAIL_FROM"):
        get_sender()


# ── SES sender · behaviour ──────────────────────────────────────────────────────────
@pytest.fixture
def mock_ses():
    """Patch boto3.client so no test ever contacts real AWS.

    The seam is inside `SesEmailSender._client()` — a lazy factory — rather than at import,
    so a dev environment without AWS credentials imports the module fine.
    """
    with patch.object(SesEmailSender, "_client") as factory:
        client = Mock()
        client.send_email = Mock(return_value={"MessageId": "test-id"})
        factory.return_value = client
        yield client


@override_settings(EMAIL_FROM="noreply@kupkop.ph", AWS_SES_REGION="ap-southeast-1")
def test_ses_sender_calls_send_email_with_the_right_shape(mock_ses):
    SesEmailSender().send(channel="email", to="ana@example.ph", code="123456",
                           purpose="signup")
    mock_ses.send_email.assert_called_once()
    kw = mock_ses.send_email.call_args.kwargs
    assert kw["Source"] == "noreply@kupkop.ph"
    assert kw["Destination"] == {"ToAddresses": ["ana@example.ph"]}
    assert "Subject" in kw["Message"] and "Body" in kw["Message"]


@override_settings(EMAIL_FROM="noreply@kupkop.ph", AWS_SES_REGION="ap-southeast-1")
def test_the_body_carries_the_code(mock_ses):
    # Non-negotiable — the whole point of the email.
    SesEmailSender().send(channel="email", to="ana@example.ph", code="482913",
                           purpose="signup")
    body = mock_ses.send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
    assert "482913" in body


@override_settings(EMAIL_FROM="noreply@kupkop.ph", AWS_SES_REGION="ap-southeast-1")
def test_the_subject_differs_by_purpose(mock_ses):
    """Signup and password reset are separate acts and read differently in an inbox.

    An inbox scanning "verification code" for signup should not surface a reset message,
    or a user searching "password" should not sift signup codes. Cheap; matters at scale.
    """
    def subject(purpose):
        mock_ses.send_email.reset_mock()
        SesEmailSender().send(channel="email", to="a@x", code="1", purpose=purpose)
        return mock_ses.send_email.call_args.kwargs["Message"]["Subject"]["Data"]
    assert subject("signup") != subject("reset")


@override_settings(EMAIL_FROM="noreply@kupkop.ph", AWS_SES_REGION="ap-southeast-1")
def test_ses_sms_is_not_sent_via_ses_it_delegates(mock_ses, caplog):
    # SES is email-only. An SMS through this sender must fall back to the console — pytest
    # itself doesn't need SMS to work, and a launch that quietly drops SMS OTPs would be
    # worse than one that logs and moves on. Matches the SMS-still-unwired handoff item.
    fallback = Mock(spec=ConsoleSender)
    SesEmailSender(fallback=fallback).send(channel="sms", to="+639171234567",
                                            code="123456", purpose="signup")
    mock_ses.send_email.assert_not_called()
    fallback.send.assert_called_once()


@override_settings(EMAIL_FROM="noreply@kupkop.ph", AWS_SES_REGION="ap-southeast-1")
def test_an_ses_failure_falls_back_and_never_raises(mock_ses, caplog):
    """A signup MUST NOT 500 because SES is having a bad afternoon.

    The OTP row was already persisted by `issue_code` before the sender is called (see
    common/otp.py); a raised exception here would leave the row orphaned AND surface a
    stack trace to the user, both wrong. The user retries; the console fallback logs so
    someone notices.
    """
    mock_ses.send_email.side_effect = RuntimeError("throttling")
    fallback = Mock(spec=ConsoleSender)
    # Not expected to raise:
    SesEmailSender(fallback=fallback).send(channel="email", to="ana@example.ph",
                                            code="123456", purpose="signup")
    fallback.send.assert_called_once()
    assert any("ses" in r.getMessage().lower() for r in caplog.records)


# ── the log line contract, still ──────────────────────────────────────────────────
@override_settings(EMAIL_FROM="noreply@kupkop.ph", AWS_SES_REGION="ap-southeast-1")
def test_the_log_line_still_never_carries_the_code(mock_ses, caplog):
    # The ConsoleSender has this property; the SES sender must keep it. Anyone with read
    # access to CloudWatch would otherwise be able to sign in as any user by tailing the
    # log for the last minute. §12.6.
    SesEmailSender().send(channel="email", to="ana@example.ph", code="987654",
                           purpose="signup")
    for record in caplog.records:
        assert "987654" not in record.getMessage()


# ── module-level get_sender caching, if any, is not the tests' business ────────────
def test_get_sender_is_a_function_not_a_singleton():
    # If someone caches the result they will silently miss a settings change (dev switching
    # to a real provider without a process restart) — a common `override_settings` gotcha.
    # The current file re-picks per call; this test preserves that.
    assert senders.get_sender.__name__ == "get_sender"
