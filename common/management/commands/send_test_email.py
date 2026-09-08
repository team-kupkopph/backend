"""Send one test email through whatever provider is currently configured.

The smoke check for §16.6 gate 3. Both real senders fall back to the ConsoleSender when the
provider errors — deliberately, so a mail outage never 500s a signup — which means a
misconfigured deploy is indistinguishable from a working one unless something asks. This
asks, and says plainly which sender answered.
"""
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from common.senders import ConsoleSender, get_sender

# Not a plausible OTP. Someone who receives this must not go looking for where to type it.
TEST_CODE = "000000"


class Command(BaseCommand):
    help = "Send a test OTP email through the configured provider, and say which one ran."

    def add_arguments(self, parser):
        parser.add_argument("to", help="Recipient address.")

    def handle(self, *args, **options):
        to = options["to"]
        try:
            sender = get_sender()
        except ImproperlyConfigured as exc:
            # A smoke check should hand back the missing variable, not a traceback.
            raise CommandError(str(exc)) from exc

        name = type(sender).__name__
        sender.send(channel="email", to=to, code=TEST_CODE, purpose="signup")

        if isinstance(sender, ConsoleSender):
            self.stdout.write(self.style.WARNING(
                f"Provider is {name} — no mail was sent. Nothing left the machine.\n"
                "Set EMAIL_PROVIDER (resend or ses) plus its credentials to send for real."))
            return

        # ⚠️ Deliberately hedged. Both senders swallow a provider failure and fall back to the
        # console, so reaching this line proves the call did not raise — NOT that the message
        # was accepted, and certainly not that it cleared spam filtering. Saying "sent!" here
        # would recreate the exact false confidence this command exists to remove.
        self.stdout.write(self.style.SUCCESS(
            f"Handed to {name} for {to} without error.\n"
            "Check the inbox (and the spam folder) to confirm delivery — a provider-side "
            "rejection is logged as a warning and falls back to the console, so no exception "
            "here does not by itself mean the mail arrived."))
