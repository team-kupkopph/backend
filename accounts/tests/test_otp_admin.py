"""US-SEC3 · the admin refuses staff with no verified TOTP device, and addstaffdevice
enrolls one. See accounts.apps.AccountsConfig.ready() for the OTPAdminSite swap and
accounts/management/commands/addstaffdevice.py for enrollment.
"""
import io

import pytest
from django.contrib import admin
from django.core.management import CommandError, call_command
from django_otp.admin import OTPAdminSite
from django_otp.plugins.otp_totp.models import TOTPDevice


# ⚠️ US-X2 removed the /admin/ route, so the two tests that drove it over HTTP are gone.
# This one stays: the OTPAdminSite swap is deliberately LEFT IN PLACE (inert without a route),
# and if the route is ever restored it must come back gated rather than open.
def test_admin_site_is_otp_gated():
    # The class-swap in AccountsConfig.ready() has already run by test collection time
    # (app registry setup happens once, at Django startup) — assert the effect, not the
    # mechanism, so this doesn't silently stop testing anything if the swap moves.
    assert isinstance(admin.site, OTPAdminSite)




@pytest.mark.django_db
def test_addstaffdevice_creates_a_confirmed_totp_device(django_user_model):
    django_user_model.objects.create_superuser("rev", "rev@kupkop.ph", "pw")
    out = io.StringIO()
    call_command("addstaffdevice", "--username", "rev", stdout=out)
    device = TOTPDevice.objects.get(user__username="rev", name="default")
    assert device.confirmed is True
    assert "otpauth://totp/" in out.getvalue()


@pytest.mark.django_db
def test_addstaffdevice_is_idempotent_and_rotates_the_key(django_user_model):
    django_user_model.objects.create_superuser("rev", "rev@kupkop.ph", "pw")
    call_command("addstaffdevice", "--username", "rev")
    old_key = TOTPDevice.objects.get(user__username="rev", name="default").key

    call_command("addstaffdevice", "--username", "rev")

    assert TOTPDevice.objects.filter(user__username="rev", name="default").count() == 1
    assert TOTPDevice.objects.get(user__username="rev", name="default").key != old_key


@pytest.mark.django_db
def test_addstaffdevice_refuses_an_unknown_username():
    with pytest.raises(CommandError):
        call_command("addstaffdevice", "--username", "nobody")


@pytest.mark.django_db
def test_addstaffdevice_refuses_a_non_staff_user(django_user_model):
    django_user_model.objects.create_user("bob", password="irrelevant")  # is_staff=False
    with pytest.raises(CommandError):
        call_command("addstaffdevice", "--username", "bob")


def test_session_and_csrf_cookies_secure_unless_debug():
    import os

    from django.conf import settings

    # Not settings.DEBUG: Django's test runner force-overrides DEBUG=False for the
    # duration of the test suite (django.test.utils.setup_test_environment), but
    # SESSION_COOKIE_SECURE/CSRF_COOKIE_SECURE were fixed at settings-import time from
    # the env var — comparing against the live (test-mutated) DEBUG would be comparing
    # against the wrong snapshot. The env var this app actually gates on is the honest
    # thing to assert against.
    debug_env = os.environ.get("DJANGO_DEBUG", "1") == "1"
    assert settings.SESSION_COOKIE_SECURE == (not debug_env)
    assert settings.CSRF_COOKIE_SECURE == (not debug_env)


def test_admin_session_age_is_short():
    from django.conf import settings

    assert settings.SESSION_COOKIE_AGE <= 3600
