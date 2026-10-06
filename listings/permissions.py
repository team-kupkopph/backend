from rest_framework.permissions import IsAuthenticated

from listings.visibility import account_is_verified_rescuer


class IsVerifiedRescuer(IsAuthenticated):
    """An approved rescuer capability (Verified Member) or an approved shelter_org
    verification (verified shelter) — the same predicate that gates public listing
    visibility (`public_poster_q`). Originally written for Sprint 3's claim gate
    (US-K1); centralised here (rather than duplicated in sagip) since this is the app
    that owns `account_is_verified_rescuer`. Sprint 4's listing/inquiry endpoints
    (US-A2/US-A4) reuse it directly — an unverified caller gets 403, which the app
    renders as the get-verified gate."""

    message = "This action requires a Verified Member badge or a verified shelter account."

    def has_permission(self, request, view):
        return (super().has_permission(request, view)
                and account_is_verified_rescuer(request.user))
