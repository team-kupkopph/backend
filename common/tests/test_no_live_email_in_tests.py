"""The suite must never talk to a real email provider.

FOUND THE HARD WAY, 2026-09-09. Putting `EMAIL_PROVIDER=resend` and a live key into `.env`
so the dev server could send mail also handed them to pytest, because tests load the same
settings module. Every OTP issued in a test then made a real HTTPS call to api.resend.com —
hundreds per run, against a 3,000/month quota, and slow enough that a timing-sensitive
throttle test started failing. That failure is what surfaced it; nothing was asserting it.

The fixture in conftest.py forces the ConsoleSender for every test. This file is the guard
that the fixture is actually doing it, so the next person who adds a provider to `.env`
does not quietly re-open a hole into a paid third-party API.
"""
from django.conf import settings

from common.senders import ConsoleSender, get_sender


def test_the_suite_resolves_to_the_console_sender():
    assert isinstance(get_sender(), ConsoleSender), (
        "tests are configured to reach a real email provider — see conftest.py")


def test_no_provider_credential_is_visible_to_a_test():
    # Not just the provider switch: a key left readable is a key that can be logged, or
    # picked up by a test that constructs a sender directly rather than via get_sender().
    assert settings.EMAIL_PROVIDER == ""
    assert settings.RESEND_API_KEY == ""
