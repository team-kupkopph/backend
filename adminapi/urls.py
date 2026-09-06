from django.urls import path

from adminapi.dashboard_views import DashboardView
from adminapi.verifications_views import (
    ApproveView,
    NeedsInfoView,
    RejectView,
    VerificationDetailView,
    VerificationQueueView,
)
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

    # US-D2 · every count the dashboard shows, in one request.
    path("dashboard", DashboardView.as_view()),

    # US-C1/C2/C3 · the verification queue. This is the workflow that gates every shelter
    # going live, which is why it is the first domain surface the console gets.
    path("verifications", VerificationQueueView.as_view()),
    path("verifications/<uuid:verification_id>", VerificationDetailView.as_view()),
    path("verifications/<uuid:verification_id>/approve", ApproveView.as_view()),
    path("verifications/<uuid:verification_id>/reject", RejectView.as_view()),
    path("verifications/<uuid:verification_id>/needs-info", NeedsInfoView.as_view()),
]
