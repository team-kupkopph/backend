"""Enroll a Kupkop reviewer's TOTP device (US-SEC3).

The admin now requires a *verified* device before it treats anyone as staff (see
accounts.apps.AccountsConfig.ready()), so this has to be a day-1 step alongside
`createstaff` — there's no self-service enrollment UI, deliberately: letting a
just-created session enroll its own second factor would defeat the point of the second
factor. An operator with shell access runs this once per reviewer and hands them the
provisioning URI (or the raw key, for manual entry) out of band.

Idempotent like createstaff: re-running replaces the named device with a fresh key
(e.g. after a lost phone), rather than erroring on the unique (user, name) pair.

    python manage.py addstaffdevice --username rev
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django_otp.plugins.otp_totp.models import TOTPDevice


class Command(BaseCommand):
    help = "Create (or replace) a confirmed TOTP device for a staff user and print its setup URI."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--device-name", dest="device_name", default="default")

    def handle(self, *, username, device_name, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(
                f"No such Django user: {username!r}. Run createstaff first.") from None
        if not user.is_staff:
            raise CommandError(f"{username!r} is not staff — createstaff sets is_staff=True.")

        # Replace rather than error-on-duplicate: a device swap (lost phone) is the
        # common case, and the old device must stop verifying once a new one exists.
        TOTPDevice.objects.filter(user=user, name=device_name).delete()
        device = TOTPDevice.objects.create(user=user, name=device_name, confirmed=True)

        self.stdout.write(self.style.SUCCESS(f"TOTP device {device_name!r} ready for {username!r}."))
        self.stdout.write(f"Scan or enter manually in an authenticator app:\n  {device.config_url}")
        self.stdout.write(f"Raw key (manual entry fallback): {device.key}")
