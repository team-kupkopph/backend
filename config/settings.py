import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me-in-production-0123456789")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

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
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    # US-SEC3 · TOTP for the Django admin. django_otp's own migrations create the device
    # tables; otp_totp is the one factor we support (no SMS/email OTP for staff — a
    # phished password alone must never be enough to reach gov IDs).
    "django_otp",
    "django_otp.plugins.otp_totp",
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
    "django.middleware.security.SecurityMiddleware",
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
        "otp_resend_min": "1/min", "otp_resend_hour": "5/hour",
        "login_ip": "20/hour", "login_identifier": "10/hour",
        "signup_ip": "10/hour",
        "password_forgot_ip": "20/hour", "password_forgot_identifier": "10/hour",
        "report_create": "20/day", "offer_create": "20/hour",
        "moderation_flag_create": "20/day",
    },
    "EXCEPTION_HANDLER": "common.errors.error_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "USER_ID_FIELD": "account_id",
    "USER_ID_CLAIM": "account_id",
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
OTP_TTL_MINUTES = 5
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

# US-SEC3 · session hardening for the reviewer surface. Gated on DEBUG rather than
# hardcoded true: the *_SECURE flags require HTTPS to even set the cookie, which the
# local/dev/test runserver never serves — hardcoding them would silently break every
# admin session (and every admin_client-based test) outside of a TLS-terminated deploy.
# SESSION_COOKIE_AGE is short because the only session-cookie consumer in this app is
# the staff admin (the mobile/API surface is JWT-only, see the INSTALLED_APPS note above).
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_AGE = 3600  # 1 hour
OTP_TOTP_ISSUER = "Kupkop PH Admin"

# US-SEC4 · RA 10173 data minimization — decided 2026-08-23: identity documents are kept
# 90 days after a terminal (approved/rejected) decision, then the file is deleted and
# verification_document.file_url is nulled; the row itself (and the decision) survives.
DOCUMENT_RETENTION_DAYS = 90

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
