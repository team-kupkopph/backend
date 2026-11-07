"""Review step 7a · the Terms of Use mechanism (Google Play's UGC policy: terms the user accepts before posting that define and prohibit
objectionable content; Apple 1.2 expects the developer's own terms to say the same).

⚠️ WHAT THIS IS, AND IS NOT. This is the MECHANISM: a published version, a way to accept it, and a gate on posting. It is not the text. The
text is the owner's and counsel's (MQ10, launch gate 6), and the gate is OFF (`TERMS_ENFORCED=0`) until it exists: switching it on over a
document nobody has reviewed would force every member to accept something that has not been checked. When counsel's text is in:
bump `TERMS_VERSION` here and in `mobile_app/src/consent.ts`, then set `TERMS_ENFORCED=1`. From then on, anyone whose accepted version is
older is asked to accept the current one the next time they try to post, and not before.

Only things people POST for others to read are gated (stories, listings, inquiries, shifts). Reporting, blocking, signing in, browsing,
and rescue reports are never gated: an animal in trouble, and a person trying to report abuse, outrank a document.
"""
from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission


class TermsNotAccepted(PermissionDenied):
    default_code = "terms_not_accepted"
    default_detail = "Please review and accept the updated Terms before you post."


def accepted(account):
    """Has `account` accepted the CURRENT terms? Always True while enforcement is off."""
    if not getattr(settings, "TERMS_ENFORCED", False):
        return True
    return bool(account) and getattr(account, "terms_consent_version", "") == settings.TERMS_VERSION


def accept(account, version):
    """Record that `account` accepted `version`. Returns False when `version` is not the current one (the document changed under them)."""
    if version != settings.TERMS_VERSION:
        return False
    account.terms_consent_at = timezone.now()
    account.terms_consent_version = version
    account.save(update_fields=["terms_consent_at", "terms_consent_version"])
    return True


class TermsAccepted(BasePermission):
    """Add beside IsAuthenticated on a view that posts content for others. A guest never reaches it (IsAuthenticated answers first)."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return True                      # not this class's question; the authentication check answers it
        if not accepted(user):
            raise TermsNotAccepted()
        return True
