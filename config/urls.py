from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health(_request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
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
