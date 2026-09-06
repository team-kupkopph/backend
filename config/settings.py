import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# US-K1 · §12.4 deployment posture.
#
# DEBUG DEFAULTS TO **OFF**. It used to default on, which is the wrong way round for
# anything that ships: a deploy that forgets to set the variable would serve tracebacks and
# a full settings dump to the public. Local work opts IN (`.env.example` sets it), because
# forgetting it locally costs you a confusing afternoon while forgetting it in production
# costs you the secret key.
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"


def _secret_key_for(debug, env_value):
    """The signing key, or a hard failure.

    There is no committed fallback any more. A default key in version control signs every
    session and JWT with a value anyone can read out of the repository, and the failure is
    silent — everything works, and it is all forgeable. Deployments must supply one; local
    work gets a throwaway that is obviously not a secret.
    """
    if env_value:
        return env_value
    if debug:
        return "dev-only-insecure-key-not-for-deployment"
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set when DEBUG is off. Generate one with: "
        "python -c 'from django.core.management.utils import get_random_secret_key as k; print(k())'")


def _allowed_hosts_for(debug, env_value):
    """The Host allowlist, or a hard failure.

    `["*"]` disables Django's Host-header validation entirely, which is what lets an
    attacker poison caches and password-reset links with a Host of their choosing. A
    wildcard is refused even when the environment explicitly asks for one — that request is
    always someone unblocking a deploy rather than making a decision.
    """
    hosts = [h.strip() for h in env_value.split(",") if h.strip()]
    if "*" in hosts:
        raise ImproperlyConfigured(
            "DJANGO_ALLOWED_HOSTS must name real hosts — '*' disables Host validation.")
    if hosts:
        return hosts
    if debug:
        return ["localhost", "127.0.0.1", "0.0.0.0", "[::1]", "testserver"]
    raise ImproperlyConfigured(
        "DJANGO_ALLOWED_HOSTS must be set when DEBUG is off (e.g. 'api.kupkop.ph').")


SECRET_KEY = _secret_key_for(DEBUG, os.environ.get("DJANGO_SECRET_KEY", ""))
ALLOWED_HOSTS = _allowed_hosts_for(DEBUG, os.environ.get("DJANGO_ALLOWED_HOSTS", ""))

# The platform ops surface (US-R1) runs on the built-in Django admin: staff (Kupkop
# reviewers) authenticate against contrib.auth.User over a web SESSION — deliberately
# separate from the app's JWT/Account identity, which the mobile clients use. The admin,
# sessions and messages apps carry the reviewer surface; the domain apps below are the API.
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",   # Sagip (US-S1) uses PostGIS: PointField + spatial queries
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    # US-SEC3 · TOTP for the Django admin. django_otp's own migrations create the device
    # tables; otp_totp is the one factor we support (no SMS/email OTP for staff — a
    # phished password alone must never be enough to reach gov IDs).
    "django_otp",
    "django_otp.plugins.otp_totp",
    # US-E2 · installed so CommonConfig.ready() can start error reporting once per process.
    # It owns no models; the analytics/observability/media/storage seams live here.
    "common",
    "accounts",
    "verifications",
    "listings",
    "shelter",
    "notifications",
    "sagip",
    "moderation",
    "volunteer",
    "devices",
    "community",
]

# SecurityMiddleware + CommonMiddleware served the JWT API alone in Sprint 1. The admin
# adds the session/auth/csrf/message/clickjacking stack. DRF's APIViews are csrf_exempt
# (they use JWT, not SessionAuthentication), so CsrfViewMiddleware does not touch the API.
MIDDLEWARE = [
    # US-E2 · FIRST, so every downstream log line and error envelope carries the id —
    # including anything raised by the security/CORS middleware below it.
    "common.observability.RequestIdMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # US-K1 · above CommonMiddleware by requirement: a CORS preflight has to be answered
    # before anything can redirect or 404 it.
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # US-SEC3 · after AuthenticationMiddleware, mirroring it: populates
    # request.user.otp_device / is_verified() from the session, which
    # accounts.apps.AccountsConfig.ready()'s OTPAdminSite swap then gates on.
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates",
             "DIRS": [], "APP_DIRS": True, "OPTIONS": {"context_processors": [
                 "django.template.context_processors.request",
                 "django.contrib.auth.context_processors.auth",
                 "django.contrib.messages.context_processors.messages",
             ]}}]

# NOTE: this is a localhost-only dev slice, not a real DATABASE_URL parser.
# We only ever read the DB name (the last "/"-separated segment) out of
# DATABASE_URL and always connect to HOST "localhost" below. Any host,
# user, password, or port encoded in a non-localhost DATABASE_URL is
# intentionally ignored here. Proper URL parsing (e.g. dj-database-url)
# is deferred until we need to point at a non-local Postgres instance.
DATABASES = {
    "default": {
        # PostGIS backend from Sprint 2 on: stray_report.geom is a real geography(Point)
        # and US-S4's map is a proximity query. The postgis backend is a superset of the
        # plain postgresql one, so every existing app keeps working. Requires the PostGIS
        # extension, enabled by the sagip 0001 migration (dev + CI + staging).
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": os.environ.get("DATABASE_URL", "postgres://localhost/kupkop_dev").rsplit("/", 1)[-1],
        "HOST": "localhost",
        "TEST": {"NAME": os.environ.get("TEST_DATABASE_NAME", "kupkop_test")},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["accounts.authentication.AccountJWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    # US-SEC2 · policy numbers, not code constants — revisit with real traffic data.
    "DEFAULT_THROTTLE_RATES": {
        # US-Q1 · the first two are per-IP; the third is per-EMAIL, so rotating IPs
        # cannot keep sending codes to one address. Sized to match the hourly IP budget:
        # a legitimate user who never receives the mail retries a handful of times, and a
        # mail-bombing script gets five messages instead of thousands.
        "otp_resend_min": "1/min", "otp_resend_hour": "5/hour",
        "otp_resend_identifier": "5/hour",
        "login_ip": "20/hour", "login_identifier": "10/hour",
        "signup_ip": "10/hour",
        "password_forgot_ip": "20/hour", "password_forgot_identifier": "10/hour",
        "report_create": "20/day", "offer_create": "20/hour",
        "moderation_flag_create": "20/day",
        "export_request": "3/day",
        # US-K2 · ceilings for the Sprint 4-6 write paths. Sized so no plausible
        # human hits them: a shelter posting a dozen needs or a donor pledging to
        # several at once stays well clear, while a script does not.
        "media_presign": "60/hour", "story_create": "10/hour",
        "need_create": "30/day", "pledge_create": "30/day",
    },
    "EXCEPTION_HANDLER": "common.errors.error_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    # ⚠️ USER_ID_FIELD / USER_ID_CLAIM are deliberately NOT set (US-K3).
    #
    # They tell SimpleJWT how to map a token claim onto `AUTH_USER_MODEL` — which here is
    # Django's `auth.User` (the staff/admin identity), NOT `Account`. This app never uses
    # that mapping: `accounts/tokens.py::tokens_for` writes the `account_id` claim itself,
    # and `accounts/authentication.py` reads it and resolves an `Account` directly.
    #
    # Setting them to "account_id" was harmless on 5.3.1 and became a bug on 5.5.1, where
    # `TokenRefreshSerializer.validate` gained a
    # `get_user_model().objects.get(**{USER_ID_FIELD: claim})` lookup — i.e. it started
    # asking the `auth_user` table for a column it has never had, and every refresh raised
    # FieldError. Left at their defaults, SimpleJWT looks for a `user_id` claim, our tokens
    # carry none, and it correctly skips a lookup that was never meaningful. The real check
    # (does this Account exist, are its sessions revoked) lives in
    # `AccountTokenRefreshSerializer` and is unaffected.
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
OTP_TTL_MINUTES = 5

# US-D1 · transactional email provider seam. Real code path, credential-guarded, silent
# dev fallback — same posture as `SENTRY_DSN` below, `MEDIA_S3_BUCKET_*` above, and the
# push seam. `common/senders.py` picks a backend from these:
#
#   EMAIL_PROVIDER unset / empty   → ConsoleSender (dev: `[DEV OTP]` prints to stdout).
#   EMAIL_PROVIDER=ses             → SesEmailSender; requires EMAIL_FROM + AWS_SES_REGION,
#                                    else settings refuse to load (loud fail on partial
#                                    config, same stance as SECRET_KEY when DEBUG is off).
#
# ⚠️ Owner actions this code cannot do: opening an AWS account, verifying `EMAIL_FROM`
# with SES, and requesting production access (SES starts in a sandbox that only mails
# verified addresses — every real signup fails until the ticket is approved). §16.6 gate 3.
EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
AWS_SES_REGION = os.environ.get("AWS_SES_REGION", "")
OTP_MAX_ATTEMPTS = 5
# Version of the Terms/Privacy text a signup consents to (RA 10173 — recorded on
# account.terms_consent_version). Bump whenever the user-facing terms change, so an
# older consent is distinguishable from consent to the current text.
TERMS_VERSION = "2026-08-01"

# D-S5-1 · the liability waiver is a versioned legal document, unlike the per-shift
# contact-sharing opt-in. Bump when the waiver text changes so an older acceptance is
# distinguishable from acceptance of the current text.
# ⚠️ The waiver TEXT does not exist yet — this version points at a document that must be
# written before Kawang-Gawa reaches real volunteers (a launch blocker for M3).
WAIVER_VERSION = "2026-08-24"

# US-SEC3 / US-K1 · session hardening + the §12.4 transport posture.
#
# Everything HTTPS-dependent is gated on `not DEBUG`: these flags need TLS to work at all,
# and a local runserver serves plain http — hardcoding them on would make an admin login
# locally impossible. `manage.py check --deploy` (asserted in
# config/tests/test_deploy_readiness.py) is what stops that gate from hiding a real gap.
#
# SESSION_COOKIE_AGE is short because the only session-cookie consumer in this app is the
# staff admin; the mobile/API surface is JWT-only (see the INSTALLED_APPS note above).
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_AGE = 3600  # 1 hour
SESSION_COOKIE_HTTPONLY = True          # the admin session is never read by script
CSRF_COOKIE_HTTPONLY = False            # Django's own CSRF flow reads this one

# Redirect http -> https at the edge. Off in DEBUG or every local request would bounce to a
# port that isn't listening.
SECURE_SSL_REDIRECT = not DEBUG
# Behind an ALB/CloudFront the app sees http; this header is how it learns the request
# actually arrived over TLS. Safe because only the load balancer can set it.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# HSTS: one year, subdomains included, preload-eligible. Set unconditionally — the header is
# only ever emitted on a secure request, so it is inert locally.
# ⚠️ Preload is close to irreversible (removal from the browser list takes months). It is
# correct here because kupkop.ph is https-only by design, but do not copy it to a domain
# that still serves anything over http.
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

SECURE_CONTENT_TYPE_NOSNIFF = True      # no MIME sniffing on user-uploaded media
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"                # the admin must never be framed (clickjacking)

# §12.4 · "reject oversized payloads". common/media.py caps the presigned OBJECT, but the
# request BODY was uncapped, so an oversized POST was parsed into memory before any view
# ran. 5 MB is generous for this API — every real payload is JSON; images go direct to S3
# through a presigned URL and never traverse the app.
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000

# §12.4 · "strict CORS allowlist". Deliberately EMPTY by default: the mobile client is a
# native app, not a browser origin, so it sends no Origin header and needs nothing here.
# Only a real web surface (an admin SPA, a marketing page calling the API) belongs on this
# list, added by hostname via the environment. CORS_ALLOW_ALL_ORIGINS is never set — it
# would hand every site on the internet the ability to make credentialed reads.
CORS_ALLOWED_ORIGINS = [o.strip() for o in
                        os.environ.get("DJANGO_CORS_ORIGINS", "").split(",") if o.strip()]
CORS_ALLOW_CREDENTIALS = False
OTP_TOTP_ISSUER = "Kupkop PH Admin"

# US-SEC4 · RA 10173 data minimization — decided 2026-08-23: identity documents are kept
# 90 days after a terminal (approved/rejected) decision, then the file is deleted and
# verification_document.file_url is nulled; the row itself (and the decision) survives.
DOCUMENT_RETENTION_DAYS = 90

# US-N2 / D-S7-1 · how long a soft-deleted account keeps its data before the purge sweep
# anonymises it irreversibly (§12.7's "grace + purge"). Read at sweep time and never stored
# on the row, so changing this changes the promise for everyone at once — which is exactly
# why it must match what the privacy policy states. The screen says "30 days".
ACCOUNT_PURGE_GRACE_DAYS = 30

# US-P2 · FCM push send seam — unset by default (no-op sender, nothing sent). Set both
# to enable the live FCM HTTP v1 transport in notifications/push.py. Never commit a
# real credentials path/secret here — supply via environment at deploy time.
FCM_PROJECT_ID = os.environ.get("FCM_PROJECT_ID", "")
FCM_CREDENTIALS_PATH = os.environ.get("FCM_CREDENTIALS_PATH", "")

# US-D2 · S3 media storage seam — two buckets by visibility (see common/storage.py).
# Unset by default (dev stub; no real object is stored or signed). Set both to activate
# the live boto3 path. Never commit bucket names that contain account IDs to VCS.
MEDIA_S3_BUCKET_PUBLIC = os.environ.get("MEDIA_S3_BUCKET_PUBLIC", "")
MEDIA_S3_BUCKET_RESTRICTED = os.environ.get("MEDIA_S3_BUCKET_RESTRICTED", "")


# US-E2 · §16.5 observability.
#
# ⚠️ `common/analytics.py::emit` has written structured JSON since Sprint 6 with NO logging
# config to route it — so every §17.2 analytics event went to Django's default handler and,
# in a deployed process, effectively nowhere. This is that missing half.
#
# One JSON object per line on stdout: the shape CloudWatch Logs Insights queries, and the
# shape a container runtime collects for free. Everything goes through the scrubber first —
# "we only log safe things" is not a claim anyone can verify about every future log call.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "common.observability.JsonFormatter"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # ⚠️ These set LEVELS ONLY and keep propagating to root, deliberately.
        #
        # Giving each its own handler with `propagate: False` looked tidier and was wrong
        # twice over: it duplicates lines the day a second handler is added, and it detaches
        # the stream from root — which silently broke the analytics tests, because a record
        # that does not propagate never reaches a root-attached capture handler either.
        #
        # Routing does not need separate handlers: every line already carries its `logger`
        # name as a queryable field, which is what a pipeline filters on.
        "kupkop.analytics": {"level": "INFO"},
        "django.request": {"level": "WARNING"},
        # Django's SQL logger at DEBUG prints every query INCLUDING bound parameters, which
        # is exactly where the PII is. Never raise this in a deployed profile.
        "django.db.backends": {"level": "WARNING"},
    },
}

# US-E2 · Sentry. Unset by default (the FCM/S3 seam posture): the code path is real and
# tested, the credential is a deploy-time task. SENTRY_RELEASE is what makes an OTA bundle
# distinguishable from the native build under it (§16.4/§16.5) — without it, a crash from an
# over-the-air update is indistinguishable from one in the binary.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
SENTRY_RELEASE = os.environ.get("SENTRY_RELEASE", "")
SENTRY_ENVIRONMENT = os.environ.get("SENTRY_ENVIRONMENT", "" if DEBUG else "production")
