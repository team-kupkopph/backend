import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _never_reach_a_real_email_provider(settings):
    """No test may talk to a live mail provider — see common/tests/test_no_live_email_in_tests.py.

    ⚠️ THIS IS NOT HYPOTHETICAL. `.env` gained `EMAIL_PROVIDER=resend` and a live API key so
    the dev server could send OTPs; tests load the same settings module, so every OTP issued
    in a test began making a real HTTPS call to api.resend.com — hundreds per run, billed
    against a 3,000/month quota, and slow enough to break a timing-sensitive throttle test.

    Blanking the credential as well as the switch is deliberate: a test that builds a sender
    directly, rather than through `get_sender()`, would otherwise still hold a usable key.
    Tests that WANT provider behaviour set it themselves with @override_settings, which still
    works — this only changes the default.
    """
    settings.EMAIL_PROVIDER = ""
    settings.RESEND_API_KEY = ""
    settings.AWS_SES_REGION = ""
    yield


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure DRF throttle buckets (keyed by client IP in Django's LocMemCache,
    which is process-lifetime and not tied to per-test DB transactions) don't
    leak between tests -- otherwise an earlier request to one throttled view
    can silently consume budget shared by another view using the same scope."""
    cache.clear()
    yield


def otp_verify(client, user):
    """US-SEC3 · mark `user`'s session as OTP-verified on `client`, the same way
    django_otp.login() does for a real admin sign-in. The admin now refuses staff
    without a verified device (accounts.apps.AccountsConfig.ready()), so any test that
    drives the admin over HTTP needs this — see the admin_client override below, which
    covers most call sites; test files that build their own staff client (e.g.
    verifications/tests/test_admin_actions.py's `staff_reviewer`) call this directly.
    """
    from django_otp import DEVICE_ID_SESSION_KEY
    from django_otp.plugins.otp_totp.models import TOTPDevice

    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return device


@pytest.fixture
def admin_client(admin_user, client):
    """Override pytest-django's admin_client: same contract (a logged-in Django
    superuser's Client), plus an OTP-verified session so US-SEC3's admin gate doesn't
    turn every existing admin_client-based test into a 302 to the login page."""
    client.force_login(admin_user)
    otp_verify(client, admin_user)
    return client
