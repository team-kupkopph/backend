"""T14a · the step-up proof: a signed, account-bound, five-minute token. No table.

`POST /me/step-up/verify` mints it after a correct one-time code; the sensitive routes read it from
`X-Step-Up`. It is a Django `TimestampSigner` value over the account id, so it cannot be used for
another account and dies on its own. Two permission classes, because "off means no prompt" and
"turning it on or off always needs a proof" are different rules:

  * `StepUpRequired`: passes while the account has two-step OFF, otherwise wants a valid proof.
  * `StepUpProof`: always wants a valid proof (enable/disable).
"""
from django.core import signing
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

HEADER = "HTTP_X_STEP_UP"
SALT = "kupkop.stepup"
TTL_SECONDS = 300


def issue_token(account):
    return signing.TimestampSigner(salt=SALT).sign(str(account.pk))


def token_is_valid(token, account):
    """True only for an unexpired token that THIS account earned. Anything else is just 'no proof'."""
    if not token or len(token) > 200:
        return False
    try:
        owner = signing.TimestampSigner(salt=SALT).unsign(token, max_age=TTL_SECONDS)
    except signing.BadSignature:                       # SignatureExpired is a BadSignature
        return False
    return owner == str(account.pk)


def _refuse():
    raise PermissionDenied(detail="Confirm it's you with a code first.", code="step_up_required")


class StepUpProof(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return True                                 # IsAuthenticated answers for that
        if token_is_valid(request.META.get(HEADER), user):
            return True
        _refuse()


class StepUpRequired(StepUpProof):
    def has_permission(self, request, view):
        user = request.user
        if user and user.is_authenticated and not user.two_factor_enabled:
            return True
        return super().has_permission(request, view)
