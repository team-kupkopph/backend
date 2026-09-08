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

import requests
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


class ResendSender(Sender):
    """Resend over its HTTP API, with the ConsoleSender as fallback for anything it can't ship.

    Chosen 2026-09-08 alongside SES rather than instead of it. SES is cheaper and already
    written, but it cannot mail an unverified address until AWS approves a sandbox-exit
    ticket — a 24h+ human review that can be refused, during which every real signup fails.
    Resend needs only an API key, so OTP delivery can start the day the key exists. Both
    live behind `get_sender()`; switching is one env var.

    The key comes from `settings.RESEND_API_KEY` (env-sourced) and is used in exactly one
    place — the Authorization header. It is never logged, never echoed into an exception
    message, and never returned. `test_the_api_key_never_reaches_a_log_line` holds that line:
    §12.6's point is that read access to logs must not become the ability to send mail as
    Kupkop.
    """

    ENDPOINT = "https://api.resend.com/emails"
    # A provider that accepts the connection and then hangs would otherwise block the worker
    # thread for the OS default — effectively forever under load. Signup is a foreground
    # request; 10s is already longer than a user will wait.
    TIMEOUT_SECONDS = 10

    def __init__(self, fallback: Sender | None = None):
        self._fallback = fallback or ConsoleSender()

    def send(self, *, channel, to, code, purpose):
        # Same reasoning as SesEmailSender: this provider is email-only, and no SMS gateway
        # exists yet. Posting an SMS code here would mail a phone number and reach nobody.
        if channel != "email":
            return self._fallback.send(channel=channel, to=to, code=code, purpose=purpose)

        masked = (to or "")[:2] + "***"
        subject, body = compose_otp_email(purpose, code)
        try:
            response = requests.post(
                self.ENDPOINT,
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                # No `html` key, deliberately — compose_otp_email documents why OTP mail is
                # plain text (fewer spam filters on a bare 6-digit code, and no tracking
                # pixel, which most HTML mail SDKs add by default).
                json={"from": settings.EMAIL_FROM, "to": [to],
                      "subject": subject, "text": body},
                timeout=self.TIMEOUT_SECONDS,
            )
            if not response.ok:
                # Deliberately NOT `raise_for_status()` + `exc_info`: a requests exception
                # carries the PreparedRequest, whose headers hold the API key. Status plus
                # the provider's own message is everything an operator needs.
                raise RuntimeError(f"resend responded {response.status_code}")
            logger.info("OTP via email to %s (sent via Resend)", masked)
        except Exception as exc:
            # A signup MUST NOT 500 because the mail provider is down or rate-limiting us.
            # The OTP row is already persisted by issue_code, so the user can resend.
            # `str(exc)` only — never the exception object's request, never exc_info.
            logger.warning({"event": "resend_send_failed", "to": masked,
                            "purpose": purpose, "detail": str(exc)[:200]})
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
    if provider == "resend":
        # Same fail-fast stance as the SES branch above, for the same reason.
        if not settings.EMAIL_FROM:
            raise ImproperlyConfigured(
                "EMAIL_PROVIDER=resend requires EMAIL_FROM (a verified sending identity).")
        if not settings.RESEND_API_KEY:
            raise ImproperlyConfigured(
                "EMAIL_PROVIDER=resend requires RESEND_API_KEY.")
        return ResendSender()
    raise ImproperlyConfigured(
        f"EMAIL_PROVIDER={provider!r} is not recognized. "
        "Known: ses, resend (or unset for dev).")
