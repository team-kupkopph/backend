from rest_framework.permissions import IsAuthenticated


class IsShelter(IsAuthenticated):
    """User+type=shelter. A signed-in account whose `account_type` is `shelter`."""

    message = "This action is only available to shelter accounts."

    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.account_type == "shelter"


class IsVerifiedShelter(IsShelter):
    """A shelter whose organisation verification is approved. Server-side twin of the
    mobile lock on "Volunteer program" (review G16 / test plan K5) — the UI gate alone let an
    unverified org publish public shifts through the API."""

    message = "Your organization must be verified before posting volunteer activities."

    def has_permission(self, request, view):
        return (super().has_permission(request, view)
                and request.user.verifications.filter(type="shelter_org",
                                                      status="approved").exists())
