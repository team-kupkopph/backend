"""US-SEC2 — abuse throttles on the public surface. Loops to the REAL configured rate
(read from settings, not hardcoded) so a rate change in config/settings.py is caught
here rather than silently drifting from what's tested.
"""
import pytest
from django.conf import settings
from django.contrib.gis.geos import Point

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from sagip.models import StrayReport


def _rate_count(scope):
    rate = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"][scope]
    return int(rate.split("/")[0])


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _assert_throttled_shape(res):
    assert res.status_code == 429
    body = res.json()
    assert body["error"]["code"] == "throttled"
    assert isinstance(body["error"]["details"]["retry_after"], int)


# ── Login: per-IP + per-identifier ──────────────────────────────────────────────────
@pytest.mark.django_db
def test_login_identifier_throttle_trips_after_the_configured_rate(client):
    # login_identifier's limit is tighter than login_ip's (see settings), so it's the
    # one that actually trips first for repeated attempts against one email.
    limit = _rate_count("login_identifier")
    for _ in range(limit):
        client.post("/api/v1/auth/login", {"email": "target@example.com", "password": "wrong"},
                    content_type="application/json")
    res = client.post("/api/v1/auth/login", {"email": "target@example.com", "password": "wrong"},
                      content_type="application/json")
    _assert_throttled_shape(res)


@pytest.mark.django_db
def test_login_throttle_behaves_identically_for_a_real_and_a_fake_email(client):
    """§12.1's enumeration asymmetry: the throttle must not let a caller learn whether
    an email exists by how differently (or whether) it gets throttled."""
    AccountFactory(email="real@example.com")
    limit = _rate_count("login_identifier")
    for email in ["real@example.com", "totally-made-up@example.com"]:
        for _ in range(limit):
            client.post("/api/v1/auth/login", {"email": email, "password": "wrong"},
                        content_type="application/json")
        res = client.post("/api/v1/auth/login", {"email": email, "password": "wrong"},
                          content_type="application/json")
        _assert_throttled_shape(res)


@pytest.mark.django_db
def test_login_identifier_throttle_is_scoped_per_email_not_global(client):
    limit = _rate_count("login_identifier")
    for _ in range(limit + 1):
        client.post("/api/v1/auth/login", {"email": "a@example.com", "password": "wrong"},
                    content_type="application/json")
    # A different email from the same IP hasn't touched ITS OWN identifier bucket yet.
    res = client.post("/api/v1/auth/login", {"email": "b@example.com", "password": "wrong"},
                      content_type="application/json")
    assert res.status_code != 429


# ── Signup: per-IP only ──────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_signup_ip_throttle_trips(client):
    limit = _rate_count("signup_ip")
    base = {"account_type": "personal", "password": "abc12345", "display_name": "X"}
    for i in range(limit):
        client.post("/api/v1/auth/signup", {**base, "email": f"u{i}@example.com"},
                    content_type="application/json")
    res = client.post("/api/v1/auth/signup", {**base, "email": "final@example.com"},
                      content_type="application/json")
    _assert_throttled_shape(res)


# ── Forgot-password: per-IP + per-identifier ──────────────────────────────────────
@pytest.mark.django_db
def test_password_forgot_identifier_throttle_trips(client):
    limit = _rate_count("password_forgot_identifier")
    for _ in range(limit):
        client.post("/api/v1/auth/password/forgot", {"email": "target@example.com"},
                    content_type="application/json")
    res = client.post("/api/v1/auth/password/forgot", {"email": "target@example.com"},
                      content_type="application/json")
    _assert_throttled_shape(res)


@pytest.mark.django_db
def test_password_forgot_throttle_identical_for_real_and_fake_email(client):
    AccountFactory(email="real2@example.com")
    limit = _rate_count("password_forgot_identifier")
    for email in ["real2@example.com", "nobody-here@example.com"]:
        for _ in range(limit):
            client.post("/api/v1/auth/password/forgot", {"email": email},
                        content_type="application/json")
        res = client.post("/api/v1/auth/password/forgot", {"email": email},
                          content_type="application/json")
        _assert_throttled_shape(res)


# ── Report creation: per-account ──────────────────────────────────────────────────
@pytest.mark.django_db
def test_report_create_throttle_trips_per_account(client):
    acc = AccountFactory()
    limit = _rate_count("report_create")
    body = {"species": "dog", "condition": "injured", "is_anonymous": False,
           "lat": 14.63, "lng": 121.05}
    for _ in range(limit):
        client.post("/api/v1/reports", body, content_type="application/json", **_hdr(acc))
    res = client.post("/api/v1/reports", body, content_type="application/json", **_hdr(acc))
    _assert_throttled_shape(res)


@pytest.mark.django_db
def test_report_create_throttle_does_not_affect_a_different_account(client):
    heavy_user = AccountFactory()
    limit = _rate_count("report_create")
    body = {"species": "dog", "condition": "injured", "is_anonymous": False,
           "lat": 14.63, "lng": 121.05}
    for _ in range(limit + 1):
        client.post("/api/v1/reports", body, content_type="application/json", **_hdr(heavy_user))

    other = AccountFactory()
    res = client.post("/api/v1/reports", body, content_type="application/json", **_hdr(other))
    assert res.status_code == 201


# ── Offer creation: per-account ───────────────────────────────────────────────────
@pytest.mark.django_db
def test_offer_create_throttle_trips_per_account(client):
    offerer = AccountFactory()
    limit = _rate_count("offer_create")
    # Distinct reports, same offer_type — UNIQUE(report, account, offer_type) means the
    # SAME report+type pair can't be offered twice by one account, so exhausting the
    # rate needs `limit` distinct reports, not `limit` calls on one.
    reports = [StrayReport.objects.create(species="dog", condition="injured", status="reported",
                                          geom=Point(121.05, 14.63, srid=4326))
              for _ in range(limit + 1)]
    for r in reports[:limit]:
        client.post(f"/api/v1/reports/{r.pk}/offers", {"offer_type": "transport"},
                   content_type="application/json", **_hdr(offerer))
    res = client.post(f"/api/v1/reports/{reports[limit].pk}/offers", {"offer_type": "transport"},
                      content_type="application/json", **_hdr(offerer))
    _assert_throttled_shape(res)


# ── IdentifierThrottle with no identifier present ─────────────────────────────────
@pytest.mark.django_db
def test_identifier_throttle_is_a_noop_with_no_identifier_in_the_body(client):
    """An empty/missing email shouldn't crash the throttle — it just means THIS
    throttle doesn't apply (the sibling per-IP throttle still does, and the serializer
    will 400 the missing field regardless)."""
    res = client.post("/api/v1/auth/login", {"password": "wrong"}, content_type="application/json")
    assert res.status_code in (400, 401)  # never a throttle-related crash


# ── OTP resend: the one throttle §15.3 names by name ────────────────────────────────
# US-Q1's §15.3 audit found this endpoint had a configured throttle and NO test — the
# only throttle in the app in that state, and the only one the spec calls out ("OTP
# throttling & expiry"). Writing the missing tests is what surfaced the scoping hole
# below; the audit's value was in the writing, not in the counting.
@pytest.mark.django_db
def test_otp_resend_trips_the_per_minute_throttle(client):
    limit = _rate_count("otp_resend_min")
    for _ in range(limit):
        client.post("/api/v1/auth/email/resend", {"email": "someone@example.com"},
                    content_type="application/json")
    res = client.post("/api/v1/auth/email/resend", {"email": "someone@example.com"},
                      content_type="application/json")
    _assert_throttled_shape(res)


@pytest.mark.django_db
def test_otp_resend_is_throttled_per_email_not_only_per_ip(client):
    """A rotating-IP caller must not be able to keep mailing codes at one address.

    This is the exact hole `IdentifierThrottle`'s own docstring describes — "an attacker
    rotating IPs still hammers one target account under IP-only throttling" — and until
    US-Q1 the resend endpoint was the one public write path still IP-only, because it
    predates those base classes and was never migrated onto them. The consequence is not
    an account takeover: it is that anyone can use Kupkop to mail-bomb a stranger's inbox,
    with our sending domain on every message.
    """
    AccountFactory(email="victim@example.com", email_verified_at=None)
    limit = _rate_count("otp_resend_identifier")
    for i in range(limit):
        client.post("/api/v1/auth/email/resend", {"email": "victim@example.com"},
                    content_type="application/json", REMOTE_ADDR=f"10.0.0.{i + 1}")
    res = client.post("/api/v1/auth/email/resend", {"email": "victim@example.com"},
                      content_type="application/json", REMOTE_ADDR="10.0.0.200")
    _assert_throttled_shape(res)


@pytest.mark.django_db
def test_otp_resend_throttle_is_identical_for_a_real_and_an_unknown_email(client):
    """§12.1 · the resend endpoint answers 202 either way; the throttle must not undo
    that by behaving differently for an address that exists."""
    AccountFactory(email="real2@example.com", email_verified_at=None)
    limit = _rate_count("otp_resend_identifier")
    seen = []
    for n, email in enumerate(["real2@example.com", "nobody@example.com"]):
        for i in range(limit):
            client.post("/api/v1/auth/email/resend", {"email": email},
                        content_type="application/json", REMOTE_ADDR=f"10.{n}.0.{i + 1}")
        res = client.post("/api/v1/auth/email/resend", {"email": email},
                          content_type="application/json", REMOTE_ADDR=f"10.{n}.0.200")
        _assert_throttled_shape(res)
        body = res.json()["error"]
        # Everything except the correlation id, which US-E2 makes per-request on purpose
        # and which therefore carries no information about the address.
        body.pop("request_id", None)
        seen.append(body)
    assert seen[0] == seen[1]
