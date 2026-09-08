"""`manage.py send_test_email` · the smoke check for a newly-configured mail provider.

WHY THIS EXISTS. Setting EMAIL_PROVIDER + a key is the easy half; knowing it WORKS is the
half that gets skipped, because the only other way to find out is to sign up a real account
and wait. Worse, both senders fall back to the ConsoleSender on failure — by design, so a
provider outage never 500s a signup — which means a misconfigured deploy looks exactly like
a working one from the outside. This command is the one place that says so out loud.
"""
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings


@override_settings(EMAIL_PROVIDER="", EMAIL_FROM="")
def test_it_names_the_console_sender_rather_than_implying_mail_was_sent():
    out = StringIO()
    call_command("send_test_email", "ana@example.ph", stdout=out)
    text = out.getvalue()
    # The whole trap this command exists to avoid: "it printed OK so email works".
    assert "ConsoleSender" in text
    assert "no mail was sent" in text.lower()


@override_settings(EMAIL_PROVIDER="resend", EMAIL_FROM="noreply@kupkop.ph",
                   RESEND_API_KEY="re_test")
def test_it_reports_the_provider_and_the_recipient_on_success():
    out = StringIO()
    with patch("common.senders.ResendSender.send") as send:
        call_command("send_test_email", "ana@example.ph", stdout=out)
    send.assert_called_once()
    assert send.call_args.kwargs["channel"] == "email"
    assert send.call_args.kwargs["to"] == "ana@example.ph"
    assert "ResendSender" in out.getvalue()


@override_settings(EMAIL_PROVIDER="resend", EMAIL_FROM="", RESEND_API_KEY="re_test")
def test_a_half_configured_provider_is_reported_as_a_command_error():
    # get_sender() raises ImproperlyConfigured; an operator running a smoke check should get
    # a clean one-line reason, not a traceback they have to read.
    with pytest.raises(CommandError, match="EMAIL_FROM"):
        call_command("send_test_email", "ana@example.ph")


@override_settings(EMAIL_PROVIDER="resend", EMAIL_FROM="noreply@kupkop.ph",
                   RESEND_API_KEY="re_test")
def test_the_test_code_is_obviously_not_a_real_otp():
    with patch("common.senders.ResendSender.send") as send:
        call_command("send_test_email", "ana@example.ph", stdout=StringIO())
    # A recipient who gets this must not mistake it for a code to type in somewhere.
    assert send.call_args.kwargs["code"] == "000000"
