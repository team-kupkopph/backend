from django.urls import path

from verifications.dev_views import MeVerificationsSeedView
from verifications.views import (
                                 MeVerificationsView,
                                 PresignView,
                                 ResubmitDocumentView,
                                 ShelterUpgradeView,
                                 VerificationCreateView,
)

urlpatterns = [
    path("media/presign", PresignView.as_view()),
    path("verifications", VerificationCreateView.as_view()),
    path("verifications/upgrade", ShelterUpgradeView.as_view()),   # US-X4 · tier-1 -> tier-2
    path("verifications/<uuid:verification_id>/documents", ResubmitDocumentView.as_view()),
    path("me/verifications", MeVerificationsView.as_view()),
    path("me/verifications/dev/seed", MeVerificationsSeedView.as_view()),   # F16 · dev-only seed
]
