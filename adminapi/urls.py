from django.urls import path

from adminapi.views import (
    StaffLoginView,
    StaffLogoutView,
    StaffPasswordResetConfirmView,
    StaffPasswordResetView,
    StaffRefreshView,
    StaffVerifyOtpView,
)

# Every route here is mounted under /admin-api/ by config/urls.py.
urlpatterns = [
    path("auth/login", StaffLoginView.as_view()),
    path("auth/verify-otp", StaffVerifyOtpView.as_view()),
    path("auth/refresh", StaffRefreshView.as_view()),
    path("auth/logout", StaffLogoutView.as_view()),
    path("auth/password-reset", StaffPasswordResetView.as_view()),
    path("auth/password-reset/confirm", StaffPasswordResetConfirmView.as_view()),
]
