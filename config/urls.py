# US-X2 (2026-09-06) · the Django admin route is GONE. Platform ops runs on the console
# (repo `admin_console`) against /admin-api/*; /admin/ now 404s.
#
# ⚠️ `django.contrib.admin` REMAINS in INSTALLED_APPS. Removing the app would drop
# django_admin_log and the admin's own migrations — a schema change nobody asked for and a
# messy revert. Removing the ROUTE removes the surface, which is the whole requirement.
#
# ⚠️ The OTPAdminSite class swap in accounts/apps.py also remains. It is inert with no route,
# and taking it out in the same change would couple two unrelated risks.
#
# ROLLBACK IS THIS ONE COMMIT: reverting it restores the admin exactly as it was, because the
# three ModelAdmin classes were kept (unregistered) rather than deleted.
from django.http import JsonResponse
from django.urls import include, path


def health(_request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("api/v1/health", health),
    path("api/v1/", include("accounts.urls")),
    path("api/v1/", include("verifications.urls")),
    path("api/v1/", include("listings.urls")),
    path("api/v1/", include("shelter.urls")),
    path("api/v1/", include("notifications.urls")),
    path("api/v1/", include("sagip.urls")),
    path("api/v1/", include("moderation.urls")),
    path("api/v1/", include("volunteer.urls")),
    path("api/v1/", include("devices.urls")),
    path("api/v1/", include("community.urls")),
    # US-B1 · the platform-ops console. Mounted at its own prefix, NOT under /api/v1/,
    # because it is a different audience on a different identity: /api/v1/* authenticates an
    # Account (mobile JWT), /admin-api/* authenticates a Django staff User bridged to an
    # admin Account. Sharing a prefix would make it far too easy for a route to end up on the
    # wrong side of that line.
    path("admin-api/", include("adminapi.urls")),
]
