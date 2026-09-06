"""Staff auth endpoints for the console (US-B1).

    POST /admin-api/auth/login      {email, password} -> {otp_required, challenge}
    POST /admin-api/auth/verify-otp {challenge, code} -> {access, refresh}
    POST /admin-api/auth/refresh    {refresh}         -> {access, refresh}  (rotated)
    POST /admin-api/auth/logout     {refresh}         -> 204
    POST /admin-api/auth/password-reset         {email}           -> 204 always
    POST /admin-api/auth/password-reset/confirm {uid, token, password} -> 204
"""
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from adminapi.auth import (
    STAFF_REFRESH_LIFETIME,
    StaffAuthError,
    issue_challenge,
    resolve_challenge,
    start_login,
    tokens_for_staff,
    verify_totp,
)
from adminapi.permissions import IsStaffJWT, StaffJWTAuthentication
from adminapi.throttles import (
    StaffLoginIdentifierThrottle,
    StaffLoginIpThrottle,
    StaffOtpIpThrottle,
)


def _error(exc: StaffAuthError):
    return Response({"error": {"code": exc.code}}, status=exc.status)


class StaffLoginView(APIView):
    """Step 1 of 2. Never mints a token — a correct password alone reaches nothing."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [StaffLoginIpThrottle, StaffLoginIdentifierThrottle]

    def post(self, request):
        try:
            user = start_login(request.data.get("email", ""), request.data.get("password", ""))
        except StaffAuthError as exc:
            return _error(exc)
        return Response({"otp_required": True, "challenge": issue_challenge(user)})


class StaffVerifyOtpView(APIView):
    """Step 2 of 2. This is the only place a staff token is minted."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [StaffOtpIpThrottle]

    def post(self, request):
        try:
            uid = resolve_challenge(request.data.get("challenge", ""))
            user = User.objects.filter(id=uid, is_active=True, is_staff=True).first()
            if user is None:
                raise StaffAuthError("invalid_challenge")
            verify_totp(user, str(request.data.get("code", "")))
            return Response(tokens_for_staff(user))
        except StaffAuthError as exc:
            return _error(exc)


class StaffRefreshView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        try:
            old = RefreshToken(request.data.get("refresh", ""))
        except TokenError:
            return Response({"error": {"code": "invalid_token"}}, status=401)

        staff_user_id = old.get("staff_user_id")
        admin_account_id = old.get("admin_account_id")
        if staff_user_id is None or admin_account_id is None:
            # An Account (mobile) refresh must not be exchangeable for a staff token.
            return Response({"error": {"code": "not_a_staff_token"}}, status=401)

        user = User.objects.filter(id=staff_user_id, is_active=True, is_staff=True).first()
        if user is None:
            return Response({"error": {"code": "staff_user_not_found"}}, status=401)

        # ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION are already True in settings, so
        # blacklisting the presented token is what makes a replay detectable.
        try:
            old.blacklist()
        except AttributeError:            # blacklist app absent — nothing to do
            pass

        try:
            return Response(tokens_for_staff(user))
        except StaffAuthError as exc:
            return _error(exc)


class StaffLogoutView(APIView):
    # ⚠️ authentication_classes is NOT optional here. DRF's global default is
    # AccountJWTAuthentication (settings.py:104), so a view that sets only permission_classes
    # authenticates with the MOBILE authenticator — which cannot read a staff token. Every
    # authenticated /admin-api/* view must name StaffJWTAuthentication explicitly; US-B3's
    # route-gating test enumerates the URLconf and asserts exactly that.
    authentication_classes = [StaffJWTAuthentication]
    permission_classes = [IsStaffJWT]

    def post(self, request):
        try:
            RefreshToken(request.data["refresh"]).blacklist()
        except Exception:
            pass
        return Response(status=204)


class StaffPasswordResetView(APIView):
    """Always 204. The endpoint must not reveal whether an email is a staff account."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [StaffLoginIpThrottle, StaffLoginIdentifierThrottle]

    def post(self, request):
        email = (request.data.get("email") or "").strip()
        user = User.objects.filter(email__iexact=email, is_active=True, is_staff=True).first()
        if user is not None:
            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            # Delivery is the console's own mail path (US-T1). The token pair is what matters
            # here; wrapping Django's generator means no bespoke token scheme to get wrong.
            request._reset_link = {"uid": uid, "token": token}   # noqa: SLF001 — test seam
        return Response(status=204)


class StaffPasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        uid = request.data.get("uid", "")
        token = request.data.get("token", "")
        password = request.data.get("password", "")
        try:
            pk = force_str(urlsafe_base64_decode(uid))
        except Exception:
            return Response({"error": {"code": "invalid_token"}}, status=400)

        user = User.objects.filter(pk=pk, is_active=True, is_staff=True).first()
        if user is None or not default_token_generator.check_token(user, token):
            return Response({"error": {"code": "invalid_token"}}, status=400)
        if len(password) < 12:
            return Response({"error": {"code": "password_too_short"}}, status=400)

        user.set_password(password)
        user.save(update_fields=["password"])
        # ⚠️ A reset changes the password ONLY. It never clears or bypasses the TOTP
        # requirement — otherwise a reset link alone would be a complete path into the
        # console, which is exactly the single-factor route US-SEC3 exists to close.
        return Response(status=204)
