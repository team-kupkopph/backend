from django.urls import path

from adminapi.dashboard_views import DashboardView
from adminapi.members_views import (
    MemberDetailView,
    MemberQueueView,
    ReinstateView,
    SuspendView,
)
from adminapi.staff_views import StaffDetailView, StaffListView, StaffResetTotpView
from adminapi.shelters_views import (
    DonationQrQueueView,
    ShelterDetailView,
    ShelterQueueView,
    UnverifyQrView,
    VerifyQrView,
)
from adminapi.moderation_views import (
    FlagActionView,
    FlagDetailView,
    FlagDismissView,
    FlagQueueView,
    FlagReviewView,
)
from adminapi.verifications_views import (
    ApproveView,
    NeedsInfoView,
    RejectView,
    VerificationDetailView,
    VerificationQueueView,
)
from adminapi.views import (
    StaffConfirmTotpView,
    StaffEnrolTotpView,
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
    # US-T1 · first-run enrolment. Without these a newly created staffer can never sign in.
    path("auth/enrol-totp", StaffEnrolTotpView.as_view()),
    path("auth/confirm-totp", StaffConfirmTotpView.as_view()),
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

    # US-M1/M2 · the moderation queue. Required before cutover: ModerationFlag is one of the
    # three registered model admins US-X1 demands a console route for.
    path("flags", FlagQueueView.as_view()),
    path("flags/<uuid:flag_id>", FlagDetailView.as_view()),
    path("flags/<uuid:flag_id>/review", FlagReviewView.as_view()),
    path("flags/<uuid:flag_id>/action", FlagActionView.as_view()),
    path("flags/<uuid:flag_id>/dismiss", FlagDismissView.as_view()),

    # US-E1/E2 · members.
    path("members", MemberQueueView.as_view()),
    path("members/<uuid:account_id>", MemberDetailView.as_view()),
    path("members/<uuid:account_id>/suspend", SuspendView.as_view()),
    path("members/<uuid:account_id>/reinstate", ReinstateView.as_view()),

    # US-S1 · shelters.
    path("shelters", ShelterQueueView.as_view()),
    path("shelters/<uuid:shelter_profile_id>", ShelterDetailView.as_view()),

    # US-Q1 · donation QRs.
    path("donation-qrs", DonationQrQueueView.as_view()),
    path("donation-qrs/<uuid:donation_qr_id>/verify", VerifyQrView.as_view()),
    path("donation-qrs/<uuid:donation_qr_id>/unverify", UnverifyQrView.as_view()),

    # US-T1 · team and roles. Superadmin only. Replaces the auth.User / auth.Group /
    # TOTP-device model admins — three of the five registrations US-X1 checks.
    path("staff", StaffListView.as_view()),
    path("staff/<int:user_id>", StaffDetailView.as_view()),
    path("staff/<int:user_id>/reset-totp", StaffResetTotpView.as_view()),
]
