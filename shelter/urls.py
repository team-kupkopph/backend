from django.urls import path

from shelter.views import (
                           DonationQrView,
                           ShelterDashboardView,
                           ShelterDonationQrPublicView,
                           ShelterProfileView,
)

urlpatterns = [
    path("shelter/profile", ShelterProfileView.as_view()),
    path("shelter/dashboard", ShelterDashboardView.as_view()),
    path("shelter/donation-qr", DonationQrView.as_view()),
    path("shelters/<uuid:account_id>/donation-qr", ShelterDonationQrPublicView.as_view()),
]
