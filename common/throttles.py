from rest_framework.throttling import SimpleRateThrottle


# US-SEC2 · abuse throttles on the public surface. Login/forgot-password get TWO
# throttles each — one per-IP, one per-identifier — because either alone has a hole: an
# attacker rotating IPs still hammers one target account under IP-only throttling, and
# an attacker with one IP but many candidate accounts (credential stuffing) sails past
# per-identifier throttling. Both apply; whichever trips first blocks the request.
#
# ⚠️ Enumeration asymmetry stays intact by construction (§12.1): the throttle key is the
# raw submitted string, never a DB lookup — an existing and a non-existing identifier are
# indistinguishable to the throttle, so the 429 (and its retry-after) is identical either
# way. Nothing here queries the database before deciding whether to throttle.
class IpThrottle(SimpleRateThrottle):
    """Base for a pure per-IP throttle — set `scope` on a subclass."""

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class IdentifierThrottle(SimpleRateThrottle):
    """Base for a throttle keyed on a submitted request-body field (e.g. `email`), not
    the caller — set `scope` and `field` on a subclass. No identifier in the body means
    this throttle simply doesn't apply (the sibling per-IP throttle still does)."""
    field = "email"

    def get_cache_key(self, request, view):
        identifier = (request.data.get(self.field) or "").strip().lower()
        if not identifier:
            return None
        return self.cache_format % {"scope": self.scope, "ident": identifier}


class LoginIpThrottle(IpThrottle):
    scope = "login_ip"


class LoginIdentifierThrottle(IdentifierThrottle):
    scope = "login_identifier"


class SignupIpThrottle(IpThrottle):
    scope = "signup_ip"


class PasswordForgotIpThrottle(IpThrottle):
    scope = "password_forgot_ip"


class PasswordForgotIdentifierThrottle(IdentifierThrottle):
    scope = "password_forgot_identifier"


# US-Q1 · these three predate the two base classes above and were, until the §15.3 audit,
# the app's only public write path throttled by IP ALONE — the exact hole the comment on
# IpThrottle describes. The cost is not a takeover: it is that anyone rotating IPs can use
# the resend endpoint to mail-bomb a stranger's inbox, over our sending domain, with a
# message that says Kupkop on it. Now both apply, like login and forgot-password.
class OtpResendMinuteThrottle(IpThrottle):
    scope = "otp_resend_min"


class OtpResendHourThrottle(IpThrottle):
    scope = "otp_resend_hour"


class OtpResendIdentifierThrottle(IdentifierThrottle):
    scope = "otp_resend_identifier"


class AccountScopedThrottle(SimpleRateThrottle):
    """Keyed on the authenticated account, not IP — for endpoints only a signed-in user
    can reach anyway, where the abuse vector is one account doing too much, not one
    anonymous IP. No identifier when unauthenticated: `IsAuthenticated` already rejects
    that request before the throttle would matter."""

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class ReportCreateThrottle(AccountScopedThrottle):
    scope = "report_create"


class OfferCreateThrottle(AccountScopedThrottle):
    scope = "offer_create"


class ModerationFlagCreateThrottle(AccountScopedThrottle):
    scope = "moderation_flag_create"


class ExportRequestThrottle(AccountScopedThrottle):
    """US-N3 · the widest authenticated read in the app, and a free amplification primitive
    if left open. Pinned low on purpose: portability is a right people exercise rarely."""
    scope = "export_request"


# --- US-K2 · the write paths Sprints 4-6 added without a scope (§12.4) ----------------
class MediaPresignThrottle(AccountScopedThrottle):
    """Presign hands out upload credentials. Unthrottled it is free storage for anyone with
    an account, and the cheapest amplification primitive in the API."""
    scope = "media_presign"


class StoryCreateThrottle(AccountScopedThrottle):
    """The newest public UGC surface. Unthrottled UGC is a spam feed."""
    scope = "story_create"


class NeedCreateThrottle(AccountScopedThrottle):
    scope = "need_create"


class PledgeCreateThrottle(AccountScopedThrottle):
    """A pledge is a promise a shelter plans around — a flood of them is a denial of service
    against a shelter's ability to plan, not just noise."""
    scope = "pledge_create"
