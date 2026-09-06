"""OTP delivery seam. Real code path, credential-guarded, silent fallback in dev.

Same posture as `common/observability.py::init_sentry` and `common/storage.py`'s S3 seam:
if the credentials are set, the real backend runs; if not, the dev stub runs. That way a
fresh checkout starts working without any external account, and production is opt-in via
env vars — which is exactly what §16.6's "secrets in a secrets store" clause needs.

⚠️ SES-specific limits the code cannot lift, and does not pretend to (owner actions):
  * an AWS account exists, with SES enabled in `AWS_SES_REGION`;
  * a sending identity is verified (an address, or better, the sending domain — the latter
    adds the DKIM record that non-Gmail filters look for);
  * production access is granted (SES starts in a "sandbox" that only mails verified
    recipients — every real signup fails until this ticket is approved).

The code assumes production access exists. When it does not, `send_email` raises and the
console fallback logs, so a launch that quietly goes without mail cannot happen — an
operator sees a warning per attempt.
"""
import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from common.messages import compose_otp_email

logger = logging.getLogger("kupkop.otp")


class Sender:
    def send(self, *, channel, to, code, purpose):
        raise NotImplementedError


class ConsoleSender(Sender):
    """The dev stub. Never talks to the network.

    Never puts the code in a log line (§12.6): CloudWatch read access should not double as
    "sign in as any user". Under DEBUG the raw code is printed to stdout so the developer
    can type it into the app — deliberately outside the logging system for that reason.
    """

    def send(self, *, channel, to, code, purpose):
        masked = (to or "")[:2] + "***"
        logger.info("OTP via %s to %s (code hidden in logs)", channel, masked)
        if settings.DEBUG:
            print(f"[DEV OTP] {channel} {to}: {code}", flush=True)


class SesEmailSender(Sender):
    """Amazon SES over boto3, with the ConsoleSender as fallback for anything it can't ship.

    Credentials come from the standard boto3 provider chain (env vars, task role, instance
    profile), NOT from Django settings — that's how AWS SDKs are supposed to work and how
    §16.3's OIDC role is meant to hand credentials to the running task.

    `boto3.client("ses")` is created lazily by `_client()` so that (a) dev machines that
    don't need it never pay the client-init cost, and (b) tests can patch a single method
    to avoid hitting AWS. See test_senders.py.
    """

    def __init__(self, fallback: Sender | None = None):
        self._fallback = fallback or ConsoleSender()

    def _client(self):
        import boto3
        return boto3.client("ses", region_name=settings.AWS_SES_REGION)

    def send(self, *, channel, to, code, purpose):
        # SES is email-only. SMS OTPs still ride ConsoleSender until an SMS gateway is wired
        # — a launch item under §16.6 gate 3 (Semaphore / Movider). Quietly using the wrong
        # channel would mail an SMS code to nobody.
        if channel != "email":
            return self._fallback.send(channel=channel, to=to, code=code, purpose=purpose)
        try:
            subject, body = compose_otp_email(purpose, code)
            self._client().send_email(
                Source=settings.EMAIL_FROM,
                Destination={"ToAddresses": [to]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            )
            # Same masked shape as ConsoleSender — never the code.
            logger.info("OTP via email to %s (sent via SES)", (to or "")[:2] + "***")
        except Exception:
            # A signup MUST NOT 500 because SES is throttling us or the identity isn't
            # verified. The OTP row has already been persisted by issue_code; the user gets
            # a retry. The console fallback logs so an operator sees it in CloudWatch and,
            # in dev, still prints [DEV OTP] so the sign-up can proceed.
            logger.warning({"event": "ses_send_failed", "to": (to or "")[:2] + "***",
                            "purpose": purpose}, exc_info=True)
            self._fallback.send(channel=channel, to=to, code=code, purpose=purpose)


def get_sender() -> Sender:
    """Pick a sender based on config, per-call (never a module-level singleton).

    Per-call because `@override_settings` in tests would silently miss a cached instance,
    and because a settings change in a running process (rare, but not impossible) should
    take effect on the next OTP rather than the next restart.
    """
    provider = (settings.EMAIL_PROVIDER or "").strip().lower()
    if provider == "":
        return ConsoleSender()
    if provider == "ses":
        # ⚠️ FAIL FAST, LOUD. A silent fallback here would mean an operator set
        # EMAIL_PROVIDER=ses without EMAIL_FROM, discovered by users when nothing arrives.
        if not settings.EMAIL_FROM:
            raise ImproperlyConfigured(
                "EMAIL_PROVIDER=ses requires EMAIL_FROM (a verified sending identity).")
        if not settings.AWS_SES_REGION:
            raise ImproperlyConfigured(
                "EMAIL_PROVIDER=ses requires AWS_SES_REGION (the region SES is enabled in).")
        return SesEmailSender()
    raise ImproperlyConfigured(
        f"EMAIL_PROVIDER={provider!r} is not recognized. Known: ses (or unset for dev).")
