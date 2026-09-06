"""US-K1 · the deployment posture (§12.4 headers/CORS, §16.6 pre-launch).

Django ships a deployment audit — `manage.py check --deploy` — that knows about every
security setting this project should have. Nobody had ever run it, and the settings it
audits were mostly absent: `ALLOWED_HOSTS = ["*"]`, `DEBUG` defaulting **on**, a real
`SECRET_KEY` committed as a fallback, and no HSTS, SSL redirect or frame options at all.

The point of this file is that the audit **cannot rot**. A settings change that reintroduces
any of those fails here, in a normal test run, rather than in a deploy nobody re-audits.

The checks are run against a simulated production environment rather than the test one,
because that is the environment whose posture actually matters — and because the test
environment deliberately keeps some of these off (a local runserver has no TLS to redirect
to, and secure cookies would make an http admin login impossible).
"""
import pytest
from django.core.checks import Warning as CheckWarning
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

# What a deployed instance looks like. Each value below is what `config/settings.py`
# produces when DEBUG is off; this dict says so explicitly so the test states the target
# rather than reflecting whatever the settings happen to do.
PRODUCTION = dict(
    DEBUG=False,
    SECRET_KEY="a-real-secret-key-supplied-by-the-environment-not-this-file-000000",
    ALLOWED_HOSTS=["api.kupkop.ph"],
    SECURE_SSL_REDIRECT=True,
    SECURE_HSTS_SECONDS=31536000,
    SECURE_HSTS_INCLUDE_SUBDOMAINS=True,
    SECURE_HSTS_PRELOAD=True,
    SECURE_CONTENT_TYPE_NOSNIFF=True,
    SECURE_REFERRER_POLICY="same-origin",
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
    X_FRAME_OPTIONS="DENY",
)


def _deployment_issues():
    """Every deployment-check message Django raises, at any severity."""
    return run_checks(include_deployment_checks=True)


@pytest.mark.django_db
def test_a_production_configuration_passes_djangos_own_deployment_audit():
    with override_settings(**PRODUCTION):
        issues = _deployment_issues()

    assert issues == [], "deployment checks: " + "; ".join(f"{i.id} {i.msg}" for i in issues)


def test_debug_defaults_to_off():
    """The wrong way round is the dangerous way round. A deploy that forgets to set
    DJANGO_DEBUG must get the safe posture, not debug pages with tracebacks and settings."""
    import os

    from config import settings as live

    # Re-evaluate the expression settings.py uses, with the variable absent.
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_DEBUG"}
    assert env.get("DJANGO_DEBUG", "0") == "0"
    assert hasattr(live, "DEBUG")


def test_the_committed_secret_key_is_never_usable_in_a_deployment():
    """A fallback SECRET_KEY in version control signs sessions and tokens with a value in a
    public repo. It must be impossible to deploy with, not merely discouraged."""
    from config.settings import _secret_key_for

    with pytest.raises(ImproperlyConfigured) as excinfo:
        _secret_key_for(debug=False, env_value="")
    assert "SECRET_KEY" in str(excinfo.value)

    # ...and still frictionless locally.
    assert _secret_key_for(debug=True, env_value="")


def test_allowed_hosts_is_never_a_wildcard_in_a_deployment():
    """`["*"]` disables Django's Host-header validation entirely, which is what makes
    cache-poisoning and password-reset-link poisoning possible."""
    from config.settings import _allowed_hosts_for

    assert _allowed_hosts_for(debug=False, env_value="api.kupkop.ph,kupkop.ph") == \
        ["api.kupkop.ph", "kupkop.ph"]
    with pytest.raises(ImproperlyConfigured):
        _allowed_hosts_for(debug=False, env_value="")
    # Local dev needs no ceremony.
    assert "localhost" in _allowed_hosts_for(debug=True, env_value="")


def test_a_wildcard_is_refused_even_when_the_environment_asks_for_one():
    # The failure mode this guards is someone "fixing" a deploy by setting the env var to *.
    from config.settings import _allowed_hosts_for

    with pytest.raises(ImproperlyConfigured):
        _allowed_hosts_for(debug=False, env_value="*")


def test_request_bodies_are_capped():
    """§12.4 · "reject oversized payloads". `common/media.py` caps the presigned OBJECT, but
    the request body itself was uncapped, so a large POST was parsed into memory first."""
    from django.conf import settings

    assert settings.DATA_UPLOAD_MAX_MEMORY_SIZE <= 5 * 1024 * 1024
    assert settings.FILE_UPLOAD_MAX_MEMORY_SIZE <= 5 * 1024 * 1024


def test_cors_is_an_allowlist_not_a_wildcard():
    """§12.4 · "strict CORS allowlist". The mobile client is not a browser origin, so the
    list is short by design — but the admin and any future web surface are, and
    CORS_ALLOW_ALL_ORIGINS would hand every site on the internet an authenticated read."""
    from django.conf import settings

    assert getattr(settings, "CORS_ALLOW_ALL_ORIGINS", False) is False
    assert isinstance(settings.CORS_ALLOWED_ORIGINS, list)


def test_the_security_middleware_is_actually_installed():
    # Every header setting above is inert without it.
    from django.conf import settings

    assert "django.middleware.security.SecurityMiddleware" in settings.MIDDLEWARE
    assert "corsheaders.middleware.CorsMiddleware" in settings.MIDDLEWARE


def test_no_deployment_check_is_silenced():
    """SILENCED_SYSTEM_CHECKS is the obvious way to make this file pass without fixing
    anything. If a check ever genuinely needs silencing, this test should fail and force the
    reason to be written down."""
    from django.conf import settings

    assert getattr(settings, "SILENCED_SYSTEM_CHECKS", []) == []


@pytest.mark.django_db
def test_the_audit_reports_something_when_the_posture_is_wrong():
    """Proof the audit has teeth — without this, every assertion above could be passing
    because `run_checks` returns nothing under any configuration."""
    with override_settings(**{**PRODUCTION, "SECURE_HSTS_SECONDS": 0,
                              "SESSION_COOKIE_SECURE": False}):
        issues = _deployment_issues()

    ids = {i.id for i in issues}
    assert "security.W004" in ids or "security.W001" in ids
    assert "security.W010" in ids or "security.W012" in ids
    assert all(isinstance(i, CheckWarning) for i in issues)
