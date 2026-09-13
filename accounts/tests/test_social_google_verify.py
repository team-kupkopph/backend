"""S0-06 landed: Google ID-token verification is real. Apple is still paperwork (S0-05).

The seam's docstring said the audience check is the whole point — a token verified without
pinning it to OUR client ID is a token minted for someone else's app, and its holder could sign
in here as that user. So these tests stub only the *cryptographic* step (google-auth's
`verify_oauth2_token`, which needs Google's live certs) and exercise everything we own around
it: the audience allow-list, the verified-email requirement, the configured/unconfigured split,
and the view's 401 for a token that does not check out.
"""
import pytest

from accounts import social

WEB = "845428226259-web.apps.googleusercontent.com"
IOS = "845428226259-ios.apps.googleusercontent.com"
SOMEONE_ELSES = "999999999999-other.apps.googleusercontent.com"


@pytest.fixture
def google_claims(monkeypatch, settings):
    """Configure two allowed audiences and make google-auth 'verify' to the given claims."""
    settings.GOOGLE_OAUTH_CLIENT_IDS = [WEB, IOS]

    def _set(claims=None, raises=None):
        def fake(id_token, request, audience=None, **kw):
            if raises:
                raise raises
            return claims
        monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", fake)
    return _set


def _claims(aud=IOS, **over):
    base = {"iss": "https://accounts.google.com", "aud": aud, "sub": "g-42",
            "email": "ana@example.com", "email_verified": True}
    base.update(over)
    return base


def test_google_token_for_our_ios_client_verifies(google_claims):
    google_claims(_claims(aud=IOS))
    assert social.verify_token("google", "tok") == {"sub": "g-42", "email": "ana@example.com"}


def test_google_token_for_our_web_client_verifies(google_claims):
    google_claims(_claims(aud=WEB))
    assert social.verify_token("google", "tok")["sub"] == "g-42"


def test_google_token_minted_for_another_app_is_refused(google_claims):
    # ⚠️ The assertion the seam's docstring was written for. Valid signature, valid issuer,
    # real Google user — and an audience that is not ours. Must not sign anyone in.
    google_claims(_claims(aud=SOMEONE_ELSES))
    with pytest.raises(social.SocialTokenInvalid):
        social.verify_token("google", "tok")


def test_google_token_with_unverified_email_is_refused(google_claims):
    # Email is our account identifier (decision 14); an unverified one from the provider is
    # not a verified address, and social signup skips our own code on the provider's word.
    google_claims(_claims(email_verified=False))
    with pytest.raises(social.SocialTokenInvalid):
        social.verify_token("google", "tok")


def test_google_signature_or_expiry_failure_is_refused(google_claims):
    google_claims(raises=ValueError("Token expired"))
    with pytest.raises(social.SocialTokenInvalid):
        social.verify_token("google", "tok")


def test_google_without_configured_client_ids_is_not_configured(settings):
    settings.GOOGLE_OAUTH_CLIENT_IDS = []
    with pytest.raises(social.SocialNotConfigured):
        social.verify_token("google", "tok")


def test_apple_is_still_not_configured(google_claims):
    # S0-05 is unpaid paperwork; Google being wired changes nothing for Apple.
    with pytest.raises(social.SocialNotConfigured):
        social.verify_token("apple", "tok")


@pytest.mark.django_db
def test_view_turns_a_refused_token_into_401_not_500(client, google_claims):
    google_claims(_claims(aud=SOMEONE_ELSES))
    res = client.post("/api/v1/auth/social/google", {"id_token": "tok"},
                      content_type="application/json")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_token"


@pytest.mark.django_db
def test_view_signs_in_with_a_verified_google_token(client, google_claims):
    google_claims(_claims(aud=IOS, sub="g-77", email="new.google@example.com"))
    res = client.post("/api/v1/auth/social/google", {"id_token": "tok"},
                      content_type="application/json")
    assert res.status_code == 200
    body = res.json()
    assert body["is_new"] is True
    assert body["account"]["email"] == "new.google@example.com"
    assert body["account"]["email_verified_at"] is not None   # the provider's word, no OTP
