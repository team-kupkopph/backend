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
]
