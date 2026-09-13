"""Provider token verification seam (US-A2).

STATUS 2026-09-13: **Google is wired** (S0-06 done — a Cloud project with iOS + Web OAuth
clients exists). **Apple is not** (S0-05, the paid Developer Program, is deferred to before
launch), so `verify_token("apple", …)` still raises SocialNotConfigured and the view answers a
typed 503; the app shows "coming soon" for that button. ⚠️ App Store Review Guideline 4.8
requires Sign in with Apple wherever Google is offered — a build in this state is fine for
dev and TestFlight and is a REJECTION if submitted. `dev/sprint-0-checklist.md` S0-05.

How Google is checked, and why each step is there:
  1. `google.oauth2.id_token.verify_oauth2_token` — signature against Google's published certs,
     issuer, expiry. This is the cryptographic step and the only thing tests stub.
  2. **Audience.** google-auth is called with `audience=None` and the `aud` claim is checked
     HERE against `settings.GOOGLE_OAUTH_CLIENT_IDS` — a list, because a native iOS sign-in is
     minted for the iOS client and a web/server flow for the Web client, and both are ours.
     A token verified without pinning it to OUR client IDs is a token minted for someone
     else's app, and its holder could sign in here as that user. Never skip this.
  3. **`email_verified`.** Email is the account identifier (decision 14) and social signup
     sets `email_verified_at` on the provider's word, sending no code of our own. So the
     provider's word has to actually say "verified".

Returns {"sub", "email"} — all the view consumes. Raises SocialTokenInvalid for anything that
does not check out (the view turns it into a 401), SocialNotConfigured when there is nothing
to check against (503).
"""
from django.conf import settings


class SocialNotConfigured(Exception):
    """No client ID to verify against. The view turns this into a clean 503 rather than
    letting a NotImplementedError surface as an opaque 500."""


class SocialTokenInvalid(Exception):
    """The token did not verify — bad signature, expired, wrong audience, unverified email.
    The view turns this into a 401. The reason is kept for the log, never sent to the client:
    'wrong audience' vs 'expired' is a hint an attacker does not need."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _verify_google(id_token):
    allowed = settings.GOOGLE_OAUTH_CLIENT_IDS
    if not allowed:
        raise SocialNotConfigured("google")
    if not id_token:
        raise SocialTokenInvalid("missing")
    # Local import: google-auth is only needed here, and only once S0-06 configured a client.
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token as g_id_token
    try:
        claims = g_id_token.verify_oauth2_token(id_token, g_requests.Request(), audience=None)
    except ValueError as exc:
        raise SocialTokenInvalid("signature_or_expiry") from exc
    if claims.get("aud") not in allowed:
        raise SocialTokenInvalid("audience")
    if not claims.get("email_verified"):
        raise SocialTokenInvalid("email_unverified")
    return {"sub": claims["sub"], "email": claims["email"]}


def verify_token(provider, id_token):
    """Return the provider's claims, at minimum {"sub", "email"}. Tests monkeypatch either
    this or google-auth's `verify_oauth2_token` beneath it."""
    if provider == "google":
        return _verify_google(id_token)
    # Apple: verify the JWT against Apple's JWKS with the same audience discipline — the
    # native iOS token's `aud` is the bundle id `ph.kupkop.app`, a web flow's is the Services
    # ID — once APPLE_CLIENT_ID exists. Until then, not configured.
    raise SocialNotConfigured(provider)
