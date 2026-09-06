"""US-E2 · structured logging, request correlation, and the Sentry seam (§16.5).

§16.5 asks for CloudWatch-shaped JSON logs, Sentry for errors, and enough correlation to
follow one request. None of it existed: `common/analytics.py::emit` has written JSON lines
since Sprint 6 with **no `LOGGING` config to route them**, so every §17.2 analytics event was
going to Django's default handler and, in a deployed process, nowhere useful.

⚠️ SCRUBBING IS THE POINT OF THIS MODULE, not a detail of it.

Observability is the one system whose entire job is to copy production data somewhere else.
An error tracker captures request bodies, headers and local variables by default. Enabling it
without redaction would put bearer tokens in a third-party dashboard (account takeover from a
support ticket), send a stray report's **precise coordinates** to a service that §12.5 spends
the rest of the codebase keeping them away from, and hand emails and phone numbers to a
processor the RA 10173 privacy notice never named.

A breach caused by our own logging is an avoidable §12.6 incident. So everything that leaves
this process goes through `scrub()` first — the Sentry hook AND the log formatter, because
"we only log safe things" is a claim no reviewer can verify about every future log call.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from contextvars import ContextVar

from django.conf import settings

# The current request's id. A ContextVar rather than thread-local so it survives async views,
# and it defaults to "-" so a sweep or management command still formats cleanly — those are
# exactly the runs nobody is watching when they break.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

REDACTED = "[redacted]"

# Key names whose VALUE is never safe to record, matched case-insensitively as a substring so
# `fcm_token`, `refresh_token` and `HTTP_AUTHORIZATION` are all covered without listing every
# spelling anyone might invent.
SENSITIVE_KEYS = (
    "password", "token", "secret", "authorization", "cookie", "session",
    # `refresh` on its own: this app names the refresh-token field exactly that,
    # so a substring match on "token" misses it entirely.
    "refresh", "access",
    "api_key", "apikey", "credential", "otp", "code_hash",
    # §12.5 · a report's exact pin. The API withholds it from strangers; an error event must
    # not be the back door that hands it to a third party.
    "lat", "lng", "latitude", "longitude", "geom", "coordinates",
    # RA 10173 personal data.
    "email", "phone", "display_name",
)

# Free-text patterns, because these turn up inside exception MESSAGES, not just tidy fields —
# "IntegrityError for ana@example.ph" is the realistic shape.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"\+?63\d{9,10}|\b09\d{9}\b")
BEARER_RE = re.compile(r"(?i)bearer\s+[\w\-.]+")


def _scrub_text(value: str) -> str:
    value = BEARER_RE.sub(f"Bearer {REDACTED}", value)
    value = EMAIL_RE.sub(REDACTED, value)
    return PHONE_RE.sub(REDACTED, value)


def scrub(value, _depth: int = 0):
    """Recursively redact anything unsafe to send off-box.

    Never raises. A scrubber that throws takes the error handler down with it, turning one
    failure into a total loss of error reporting — the opposite of what it is for.

    Deliberately conservative about WHAT it redacts and generous about what it keeps: an
    event where everything reads `[redacted]` is one nobody can debug, and a tool nobody can
    debug with gets switched off.
    """
    try:
        if _depth > 12:                       # cyclic or pathological input
            return value
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if isinstance(key, str) and any(s in key.lower() for s in SENSITIVE_KEYS):
                    out[key] = REDACTED
                else:
                    out[key] = scrub(item, _depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            return [scrub(v, _depth + 1) for v in value]
        if isinstance(value, str):
            return _scrub_text(value)
        return value
    except Exception:
        return REDACTED


class JsonFormatter(logging.Formatter):
    """One JSON object per line — the shape CloudWatch Logs Insights can query (§16.5).

    A payload that is already structured is NESTED rather than embedded as a string, so a
    pipeline can filter on `message.event` instead of re-parsing a quoted blob. Two forms
    count as structured, and the second was a real bug:

      * a JSON string — what `common/analytics.py::emit` writes;
      * a dict passed straight to the logger, `logger.warning({"event": ...})`, which is the
        natural way to log structured data in Python. `record.getMessage()` renders that with
        `str()`, so it arrived as Python REPR — single quotes, `None` for `null` — which
        begins with `{` and so reads like JSON to a human while failing every parser. That
        defeats the entire purpose of §16.5 while looking exactly as though it works. Caught
        by reading a real line out of the overlap guard, not by a test of this class.
    """

    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record.msg, dict) and not record.args:
            message = record.msg
        else:
            raw = record.getMessage()
            try:
                message = json.loads(raw) if raw.startswith("{") else raw
            except ValueError:
                message = raw

        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "message": scrub(message),
        }
        if record.exc_info:
            # The traceback is the useful half of an error line; it is scrubbed like
            # everything else because exception text routinely contains the value that broke.
            payload["exception"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class RequestIdMiddleware:
    """Assign (or adopt) a request id, and echo it back.

    An inbound `X-Request-ID` WINS — a load balancer or client that already assigned one is
    the thing correlating across hops, and minting a fresh id at every hop correlates nothing.

    The id is echoed in the response header and (via `common/errors.py`) in the error envelope,
    so a user can read it back off the screen. "It failed at about 3pm" is unactionable;
    an id points at the exact lines.
    """

    HEADER = "HTTP_X_REQUEST_ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get(self.HEADER, "").strip()
        request_id = incoming or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)

    def process_template_response(self, request, response):
        """Stamp the id into the error body itself, before DRF renders it.

        ⚠️ This has to happen HERE rather than only in the DRF exception handler. Most error
        bodies in this codebase are hand-built `Response({"error": ...})` returned straight
        from a view — a 409 with blockers, a 422 with a field — and those never pass through
        the exception handler at all. Stamping only there would have covered a minority of the
        errors a user can actually see, which is worse than not doing it: it would look done.
        """
        data = getattr(response, "data", None)
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            data["error"].setdefault("request_id", request_id_var.get())
        return response


def init_sentry() -> bool:
    """Start Sentry if a DSN is configured. Returns whether it started.

    Same posture as the FCM sender and the S3 storage seam: the code path is real and
    reviewable, the credential is a deploy-time task, and an unset DSN is a normal state
    rather than an error on every boot.

    Release-tagged (§16.5) so an OTA bundle is distinguishable from the native build it ran
    on — without that, a crash report from an over-the-air update looks identical to one from
    the binary underneath it, and §16.4's whole OTA strategy becomes undebuggable.
    """
    dsn = getattr(settings, "SENTRY_DSN", "")
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
    except ImportError:                       # not installed in this environment
        return False

    sentry_sdk.init(
        dsn=dsn,
        integrations=[DjangoIntegration()],
        release=getattr(settings, "SENTRY_RELEASE", "") or None,
        environment=getattr(settings, "SENTRY_ENVIRONMENT", "") or None,
        # NEVER send request bodies or local variables unscrubbed. `before_send` below is the
        # backstop, but not collecting them at all is the stronger control.
        send_default_pii=False,
        max_request_body_size="never",
        before_send=lambda event, hint: scrub(event),
    )
    return True
