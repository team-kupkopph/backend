from django.urls import path

from devices.views import DeviceTokenDetailView, DeviceTokensView

urlpatterns = [
    path("me/device-tokens", DeviceTokensView.as_view()),
    path("me/device-tokens/<uuid:token_id>", DeviceTokenDetailView.as_view()),
]
