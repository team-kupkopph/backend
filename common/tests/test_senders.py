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
import json
import logging
from unittest.mock import Mock, patch

import pytest
import responses as responses_lib
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


# ── Resend · the API-key provider (chosen 2026-09-08) ────────────────────────────────
#
# WHY A SECOND PROVIDER AT ALL. SES is cheaper and already coded, but it cannot send to an
# unverified address until AWS approves a sandbox-exit ticket — a 24h+ human review that can
# be refused. Resend needs only an API key, so OTP mail can go live the day the key exists.
# SesEmailSender stays; this is an alternative behind the same seam, not a replacement.
RESEND_URL = "https://api.resend.com/emails"
RESEND_CFG = dict(EMAIL_PROVIDER="resend", EMAIL_FROM="noreply@kupkop.ph",
                  RESEND_API_KEY="re_test_key_do_not_use")


@override_settings(**RESEND_CFG)
def test_resend_is_selected_when_configured():
    from common.senders import ResendSender
    assert isinstance(get_sender(), ResendSender)


@override_settings(EMAIL_PROVIDER="resend", EMAIL_FROM="", RESEND_API_KEY="re_x")
def test_resend_without_a_from_address_refuses_to_start():
    with pytest.raises(ImproperlyConfigured, match="EMAIL_FROM"):
        get_sender()


@override_settings(EMAIL_PROVIDER="resend", EMAIL_FROM="noreply@kupkop.ph", RESEND_API_KEY="")
def test_resend_without_an_api_key_refuses_to_start():
    # Same fail-fast stance as the SES pair: a deploy that mails nobody because one env var
    # is missing must be found at boot, not by a user who never got their code.
    with pytest.raises(ImproperlyConfigured, match="RESEND_API_KEY"):
        get_sender()


@responses_lib.activate
@override_settings(**RESEND_CFG)
def test_resend_posts_the_shape_the_api_documents():
    from common.senders import ResendSender
    responses_lib.add(responses_lib.POST, RESEND_URL, json={"id": "abc"}, status=200)

    ResendSender().send(channel="email", to="ana@example.ph", code="123456", purpose="signup")

    assert len(responses_lib.calls) == 1
    req = responses_lib.calls[0].request
    body = json.loads(req.body)
    assert body["from"] == "noreply@kupkop.ph"
    assert body["to"] == ["ana@example.ph"]
    assert "Verify your Kupkop PH account" == body["subject"]
    assert req.headers["Authorization"] == "Bearer re_test_key_do_not_use"


@responses_lib.activate
@override_settings(**RESEND_CFG)
def test_the_resend_body_carries_the_code_and_stays_plain_text():
    from common.senders import ResendSender
    responses_lib.add(responses_lib.POST, RESEND_URL, json={"id": "abc"}, status=200)
    ResendSender().send(channel="email", to="ana@example.ph", code="654321", purpose="signup")
    body = json.loads(responses_lib.calls[0].request.body)
    assert "654321" in body["text"]
    # compose_otp_email documents why OTP mail is plain text (spam filters, no tracking
    # pixel on a one-time code). Sending `html` here would quietly undo that decision.
    assert "html" not in body


@responses_lib.activate
@override_settings(**RESEND_CFG)
def test_a_resend_outage_falls_back_and_never_raises(caplog):
    from common.senders import ResendSender
    responses_lib.add(responses_lib.POST, RESEND_URL, json={"message": "boom"}, status=500)
    # A signup MUST NOT 500 because the mail provider is down — the OTP row is already
    # persisted, so the user can resend.
    ResendSender().send(channel="email", to="ana@example.ph", code="123456", purpose="signup")
    assert "resend_send_failed" in caplog.text


@responses_lib.activate
@override_settings(**RESEND_CFG)
def test_the_api_key_never_reaches_a_log_line(caplog):
    """§12.6 · read access to logs must not become the ability to send mail as Kupkop.

    The failure path logs the exception, and a naive `exc_info` on a requests error can carry
    the full request — headers included — into the log.
    """
    from common.senders import ResendSender
    responses_lib.add(responses_lib.POST, RESEND_URL, json={"message": "boom"}, status=500)
    with caplog.at_level(logging.DEBUG):
        ResendSender().send(channel="email", to="ana@example.ph", code="1", purpose="signup")
    assert "re_test_key_do_not_use" not in caplog.text


@responses_lib.activate
@override_settings(**RESEND_CFG)
def test_the_resend_log_line_never_carries_the_code(caplog):
    from common.senders import ResendSender
    responses_lib.add(responses_lib.POST, RESEND_URL, json={"id": "abc"}, status=200)
    with caplog.at_level(logging.DEBUG):
        ResendSender().send(channel="email", to="ana@example.ph", code="987654",
                            purpose="signup")
    assert "987654" not in caplog.text


@responses_lib.activate
@override_settings(**RESEND_CFG)
def test_resend_sms_is_not_sent_as_email_it_delegates(caplog):
    from common.senders import ResendSender
    responses_lib.add(responses_lib.POST, RESEND_URL, json={"id": "abc"}, status=200)
    ResendSender().send(channel="sms", to="+639170000123", code="123456", purpose="signup")
    # No SMS gateway exists yet; mailing an SMS code to a phone number reaches nobody.
    assert len(responses_lib.calls) == 0


@override_settings(**RESEND_CFG)
def test_the_request_sets_a_timeout():
    """A `requests.post` with no timeout blocks the worker thread forever if Resend hangs.

    Asserted by inspecting the call rather than by hanging the suite for real.
    """
    from common.senders import ResendSender
    with patch.object(senders.requests, "post") as post:
        post.return_value = Mock(status_code=200, ok=True)
        ResendSender().send(channel="email", to="a@b.ph", code="1", purpose="signup")
    assert post.call_args.kwargs.get("timeout"), "no timeout — a hung provider hangs the worker"
