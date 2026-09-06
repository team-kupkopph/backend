from django.urls import path

from accounts.views import (
                            AccountTokenRefreshView,
                            EmailResendView,
                            EmailVerifyView,
                            LoginView,
                            LogoutAllView,
                            LogoutView,
                            MeExportView,
                            MeLocationView,
                            MePhoneVerifyView,
                            MePhoneView,
                            MeSettingsView,
                            MeView,
                            PasswordForgotView,
                            PasswordResetView,
                            SignupView,
                            SocialAuthView,
)

urlpatterns = [
    path("auth/signup", SignupView.as_view()),
    path("auth/email/verify", EmailVerifyView.as_view()),
    path("auth/email/resend", EmailResendView.as_view()),
    path("auth/login", LoginView.as_view()),
    path("auth/refresh", AccountTokenRefreshView.as_view()),
    path("auth/logout", LogoutView.as_view()),
    path("auth/logout-all", LogoutAllView.as_view()),
    path("auth/password/forgot", PasswordForgotView.as_view()),
    path("auth/password/reset", PasswordResetView.as_view()),
    path("auth/social/<str:provider>", SocialAuthView.as_view()),
    path("me", MeView.as_view()),
    path("me/settings", MeSettingsView.as_view()),
    path("me/export", MeExportView.as_view()),
    path("me/location", MeLocationView.as_view()),
    path("me/phone", MePhoneView.as_view()),
    path("me/phone/verify", MePhoneVerifyView.as_view()),
]
