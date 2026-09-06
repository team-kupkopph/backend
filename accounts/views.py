from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from accounts.models import Account, AccountStatus
from accounts.serializers import (
    AccountTokenRefreshSerializer,
    EmailSerializer,
    EmailVerifySerializer,
    MeSettingsSerializer,
    MeUpdateSerializer,
    SignupSerializer,
    account_repr,
    me_repr,
)
from accounts.tokens import tokens_for
from common import otp
from common.otp import CodeExpired, CodeInvalid, CodeLocked, issue_code, verify_code
from common.throttles import (
    ExportRequestThrottle,
    LoginIdentifierThrottle,
    LoginIpThrottle,
    OtpResendHourThrottle,
    OtpResendIdentifierThrottle,
    OtpResendMinuteThrottle,
    PasswordForgotIdentifierThrottle,
    PasswordForgotIpThrottle,
    SignupIpThrottle,
)


class SignupView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [SignupIpThrottle]

    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        if not serializer.is_valid():
            email_errors = serializer.errors.get("email")
            if email_errors and getattr(email_errors[0], "code", None) == "email_taken":
                return Response(
                    {"error": {"code": "email_taken", "message": "Email already in use",
                               "field": "email"}}, status=status.HTTP_409_CONFLICT)
            return Response({"error": {"code": "invalid", "message": "Invalid input",
                                       "details": serializer.errors}}, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        try:
            account = Account.objects.create_account(
                account_type=data["account_type"], email=data["email"],
                password=data["password"], display_name=data["display_name"],
                terms_consent_version=data.get("consent_version") or None)
        except IntegrityError:
            return Response(
                {"error": {"code": "email_taken", "message": "Email already in use",
                           "field": "email"}}, status=status.HTTP_409_CONFLICT)
        otp.issue_code(account, channel="email", purpose="signup")
        return Response({"account_id": str(account.account_id), "email": account.email,
                         "next": "verify_email"}, status=status.HTTP_201_CREATED)


def _otp_error_response(exc):
    if isinstance(exc, CodeLocked):
        return Response({"error": {"code": "code_locked", "message": "Too many attempts"}}, status=423)
    if isinstance(exc, CodeExpired):
        return Response({"error": {"code": "code_expired", "message": "Code expired"}}, status=410)
    return Response({"error": {"code": "code_invalid", "message": "Invalid code",
                               "details": {"attempts_left": exc.attempts_left}}}, status=400)


class EmailVerifyView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        s = EmailVerifySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        account = Account.objects.filter(email=s.validated_data["email"]).first()
        if account is None:
            # Mirror the response a real account gets on its first wrong-code
            # attempt (attempts_left = OTP_MAX_ATTEMPTS - 1) so a single probe
            # can't distinguish "no such account" from "account exists, code
            # just hasn't been guessed yet".
            return _otp_error_response(CodeInvalid(attempts_left=settings.OTP_MAX_ATTEMPTS - 1))
        try:
            verify_code(account, purpose="signup", code=s.validated_data["code"])
        except (CodeInvalid, CodeExpired, CodeLocked) as exc:
            return _otp_error_response(exc)
        if account.email_verified_at is None:
            account.email_verified_at = timezone.now()
            account.save(update_fields=["email_verified_at"])
        return Response({**tokens_for(account), "account": account_repr(account)})


class EmailResendView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OtpResendMinuteThrottle, OtpResendHourThrottle,
                        OtpResendIdentifierThrottle]

    def post(self, request):
        s = EmailSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        account = Account.objects.filter(email=s.validated_data["email"]).first()
        if account is not None and account.email_verified_at is None:
            issue_code(account, channel="email", purpose="signup")
        return Response({}, status=202)   # generic regardless


class LoginView(APIView):
    permission_classes = [AllowAny]
    # US-SEC2 · both apply (IP catches credential stuffing across many accounts from one
    # source; identifier catches one target account hammered from many IPs). Neither
    # throttle queries the DB, so the enumeration asymmetry (§12.1) stays intact — the
    # 429 is identical whether or not the submitted email exists.
    throttle_classes = [LoginIpThrottle, LoginIdentifierThrottle]

    def post(self, request):
        email = request.data.get("email", "")
        password = request.data.get("password", "")
        account = Account.objects.filter(email=email).first()
        # US-N1 · a deleted account is refused here, in the SAME branch and with the SAME
        # body as a wrong password. A distinct code or message would turn deletion into an
        # enumeration oracle — §12.1's whole point is that login reveals nothing.
        if (account is None or account.status == AccountStatus.DELETED
                or not account.check_password(password)):
            return Response({"error": {"code": "invalid_credentials",
                                       "message": "Email or password is incorrect"}}, status=401)
        if account.email_verified_at is None:
            issue_code(account, channel="email", purpose="signup")
            return Response({"error": {"code": "email_unverified",
                                       "message": "Verify your email"}}, status=403)
        return Response({**tokens_for(account), "account": account_repr(account)})


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            RefreshToken(request.data["refresh"]).blacklist()
        except Exception:
            pass

        fcm = (request.data.get("fcm_token") or "").strip()
        if fcm:
            from devices.models import DeviceToken
            DeviceToken.objects.filter(account=request.user, fcm_token=fcm).delete()

        return Response(status=204)


class LogoutAllView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        request.user.sessions_revoked_at = timezone.now()
        request.user.save(update_fields=["sessions_revoked_at"])

        from devices.models import DeviceToken
        DeviceToken.objects.filter(account=request.user).delete()

        return Response(status=204)


class AccountTokenRefreshView(TokenRefreshView):
    serializer_class = AccountTokenRefreshSerializer


class PasswordForgotView(APIView):
    permission_classes = [AllowAny]
    # US-SEC2 · was riding the generic OTP-resend buckets (1/min, 5/hr — tuned for a
    # different threat: running up SMS/email cost via resend-spam on ONE code). Forgot-
    # password is closer to credential-stuffing/enumeration abuse, so it gets its own
    # scope at a wider rate, on the same IP+identifier pair as login.
    throttle_classes = [PasswordForgotIpThrottle, PasswordForgotIdentifierThrottle]

    def post(self, request):
        s = EmailSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        account = Account.objects.filter(email=s.validated_data["email"]).first()
        if account is not None:
            issue_code(account, channel="email", purpose="reset")
        return Response({})   # always generic


class PasswordResetView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        from accounts.serializers import PasswordResetSerializer
        s = PasswordResetSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        account = Account.objects.filter(email=s.validated_data["email"]).first()
        if account is None:
            # Mirror the response a real account gets on its first wrong-code
            # attempt so a single probe can't distinguish "no such account"
            # from "account exists, code just hasn't been guessed yet".
            return _otp_error_response(CodeInvalid(attempts_left=settings.OTP_MAX_ATTEMPTS - 1))
        try:
            verify_code(account, purpose="reset", code=s.validated_data["code"])
        except (CodeInvalid, CodeExpired, CodeLocked) as exc:
            return _otp_error_response(exc)
        account.set_password(s.validated_data["new_password"])
        account.sessions_revoked_at = timezone.now()   # revoke all existing sessions (Task 6 mechanism)
        account.save(update_fields=["password_hash", "sessions_revoked_at"])
        return Response({})


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(me_repr(request.user))

    def delete(self, request):
        """US-N1 · §12.6's erasure right. Soft delete + immediate session revocation;
        the PII scrub follows after the grace window (accounts/purge.py, D-S7-1)."""
        from accounts.lifecycle import open_commitments, soft_delete_account

        if request.data.get("confirm") is not True:
            return Response({"error": {"code": "confirmation_required",
                                       "message": "Confirm to delete your account.",
                                       "field": "confirm"}}, status=400)

        blockers = open_commitments(request.user)
        if blockers:
            # Other people are relying on these — the refusal names every one of them so the
            # screen can list what to close rather than dead-ending on "you have commitments".
            return Response({"error": {
                "code": "has_active_commitments",
                "message": "Close these before deleting your account.",
                "details": {"blockers": blockers},
            }}, status=409)

        soft_delete_account(request.user)
        return Response(status=204)

    def patch(self, request):
        acc = request.user
        s = MeUpdateSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        if "display_name" in s.validated_data:
            acc.display_name = s.validated_data["display_name"]
        if "photo_file_url" in s.validated_data:
            acc.photo_url = s.validated_data["photo_file_url"]
        acc.save()
        return Response(me_repr(acc))


class MeSettingsView(APIView):
    permission_classes = [IsAuthenticated]
    FIELDS = ["marketing_emails", "approximate_location", "masked_contact", "push_enabled",
              "analytics_consent"]

    def _repr(self, s):
        body = {f: getattr(s, f) for f in self.FIELDS}
        # Read-only alongside the flag: the client renders "off by default, you may withdraw",
        # and the controller can show WHEN consent was given (§12.6).
        body["analytics_consent_at"] = (s.analytics_consent_at.isoformat()
                                        if s.analytics_consent_at else None)
        return body

    def get(self, request):
        return Response(self._repr(request.user.settings))

    def patch(self, request):
        s = request.user.settings
        serializer = MeSettingsSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        # D-S7-3 · the stamp moves only on a real CHANGE of mind. Re-sending the same value
        # is an idempotent PATCH, not a fresh act of consent, and must not restamp;
        # withdrawing clears the stamp so the column never describes a lapsed consent.
        if "analytics_consent" in data and data["analytics_consent"] != s.analytics_consent:
            s.analytics_consent_at = timezone.now() if data["analytics_consent"] else None
        for key, value in data.items():
            setattr(s, key, value)
        s.save()
        return Response(self._repr(s))


class MeExportView(APIView):
    """US-N3 · GET /me/export — RA 10173 portability, synchronously (D-S7-2).

    Returned as a download rather than a bare body so the mobile client can hand it to the
    OS share sheet without inventing a filename. The contents rule lives in
    `accounts/export.py`: the caller's data and nobody else's.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ExportRequestThrottle]

    def get(self, request):
        from accounts.export import build_export, export_filename

        response = Response(build_export(request.user))
        response["Content-Disposition"] = f'attachment; filename="{export_filename()}"'
        return response


class MeLocationView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        from accounts.models import Address
        city = request.data.get("city")
        barangay = request.data.get("barangay", "")
        if not city:
            return Response({"error": {"code": "invalid", "message": "city required",
                                       "field": "city"}}, status=400)
        addr, _ = Address.objects.get_or_create(account=request.user, is_primary=True,
                                                defaults={"city": city})
        addr.city = city
        addr.barangay = barangay
        addr.geom = None
        addr.save()
        return Response({"city": addr.city, "barangay": addr.barangay or None})


class MePhoneView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        phone = (request.data.get("phone") or "").strip()
        if not phone:
            return Response({"error": {"code": "invalid", "message": "phone required",
                                       "field": "phone"}}, status=400)
        acc = request.user
        # Store the candidate now (phone is nullable with a separate phone_verified_at);
        # verification only sets phone_verified_at. A number already verified/held by
        # another account collides on the UNIQUE column -> generic phone_taken.
        if Account.objects.filter(phone=phone).exclude(pk=acc.pk).exists():
            return Response({"error": {"code": "phone_taken",
                                       "message": "That number can't be used"}}, status=409)
        acc.phone = phone
        acc.phone_verified_at = None
        try:
            acc.save(update_fields=["phone", "phone_verified_at"])
        except IntegrityError:
            return Response({"error": {"code": "phone_taken",
                                       "message": "That number can't be used"}}, status=409)
        issue_code(acc, channel="sms", purpose="phone")
        return Response({}, status=202)


class MePhoneVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        acc = request.user
        try:
            verify_code(acc, purpose="phone", code=request.data.get("code", ""))
        except (CodeInvalid, CodeExpired, CodeLocked) as exc:
            return _otp_error_response(exc)
        acc.phone_verified_at = timezone.now()
        acc.save(update_fields=["phone_verified_at"])
        return Response({"phone_verified_at": acc.phone_verified_at})


class SocialAuthView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, provider):
        from accounts import social
        from accounts.models import AccountIdentity
        if provider not in ("google", "apple"):
            return Response({"error": {"code": "unsupported_provider",
                                       "message": "That sign-in provider isn't supported"}}, status=400)
        try:
            claims = social.verify_token(provider, request.data.get("id_token"))
        except social.SocialNotConfigured:
            # Not wired yet (see accounts/social.py). A clean, typed 503 beats a 500 —
            # the client can show "not available yet" instead of "something went wrong".
            return Response({"error": {"code": "social_not_configured",
                                       "message": "Social sign-in isn't available yet"}}, status=503)
        email = claims.get("email")
        if not email:
            return Response({"error": {"code": "email_required",
                                       "message": "This provider did not share an email address"}},
                            status=400)
        sub = claims["sub"]
        identity = AccountIdentity.objects.filter(provider=provider, provider_user_id=sub).first()
        is_new = False
        if identity:
            account = identity.account
        else:
            account = Account.objects.filter(email=email).first()
            if account is None:
                at = request.data.get("account_type", "personal")
                if at not in ("personal", "shelter"):
                    at = "personal"
                account = Account.objects.create_account(
                    account_type=at,
                    email=email, display_name=email.split("@")[0], password=None)
                account.email_verified_at = timezone.now()
                account.save(update_fields=["email_verified_at"])
                is_new = True
            AccountIdentity.objects.create(account=account, provider=provider,
                                           provider_user_id=sub, email=email)
        return Response({**tokens_for(account), "is_new": is_new,
                         "account": account_repr(account)})
