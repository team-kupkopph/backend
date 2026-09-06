import re

from rest_framework import serializers
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken as SimpleRefreshToken

from accounts.models import Account

# Canonical signup/reset password rule (dev/onboarding-validation.md §signup): min 8 chars,
# at least one number. This is the ONLY enforcement — the AUTH_PASSWORD_VALIDATORS in settings
# are never invoked, because DRF serializers don't call Django's validate_password. One error
# string, matching the screen copy, so client and server say the same thing.
PASSWORD_ERROR = "At least 8 characters, including a number."


def validate_password_strength(value):
    if len(value) < 8 or not re.search(r"\d", value):
        raise serializers.ValidationError(PASSWORD_ERROR)
    return value


class SignupSerializer(serializers.Serializer):
    # Public signup may only create "personal" or "shelter" accounts.
    # "admin" (in AccountType.choices) must never be reachable from here.
    account_type = serializers.ChoiceField(choices=[("personal", "personal"), ("shelter", "shelter")])
    display_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, validators=[validate_password_strength])
    # The terms version the client actually displayed. Optional so an older build still
    # signs up (the server falls back to settings.TERMS_VERSION) — but when the client
    # sends it, we record what the user was really shown rather than what we assume.
    consent_version = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_email(self, value):
        if Account.objects.filter(email=value).exists():
            raise serializers.ValidationError("email_taken", code="email_taken")
        return value


def account_repr(account):
    return {
        "account_id": str(account.account_id),
        "account_type": account.account_type,
        "display_name": account.display_name,
        "email": account.email,
        "email_verified_at": account.email_verified_at,
        "phone": account.phone,
        "phone_verified_at": account.phone_verified_at,
        "photo_url": account.photo_url or None,
    }


def me_repr(account):
    rel = getattr(account, "capabilities", None)
    caps = [{"capability": c.capability, "status": c.status} for c in rel.all()] if rel else []
    s = account.settings
    # Local import: shelter depends on accounts, so importing it at module load would
    # risk an app-registry cycle. `shelter` is null for a personal account (the client
    # picks the owner shell); a shelter account carries its tier + derived status.
    from shelter.models import ShelterProfile
    profile = ShelterProfile.objects.filter(account=account).first()
    shelter = None
    if profile is not None:
        vr = account.verifications.filter(type="shelter_org").order_by("-submitted_at").first()
        shelter = {"tier": profile.tier, "verification_status": vr.status if vr else None}
    return {**account_repr(account), "capabilities": caps, "shelter": shelter,
            "settings": {"marketing_emails": s.marketing_emails,
                         "approximate_location": s.approximate_location,
                         "masked_contact": s.masked_contact, "push_enabled": s.push_enabled}}


class MeUpdateSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=100, required=False)
    photo_file_url = serializers.CharField(required=False, allow_blank=True)


class MeSettingsSerializer(serializers.Serializer):
    marketing_emails = serializers.BooleanField(required=False)
    approximate_location = serializers.BooleanField(required=False)
    masked_contact = serializers.BooleanField(required=False)
    push_enabled = serializers.BooleanField(required=False)
    # D-S7-3 · analytics_consent_at is NOT accepted from the client: the timestamp is the
    # controller's record of when consent was given, so the server stamps it. A client that
    # could set it could backdate its own consent.
    analytics_consent = serializers.BooleanField(required=False)


class EmailVerifySerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField()


class EmailSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField()
    new_password = serializers.CharField(validators=[validate_password_strength])


class AccountTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        token = SimpleRefreshToken(attrs["refresh"])
        account = Account.objects.filter(account_id=token.get("account_id")).first()
        if account is None:
            raise InvalidToken("account_not_found")
        if (account.sessions_revoked_at
                and int(token["iat"]) <= int(account.sessions_revoked_at.timestamp())):
            raise InvalidToken("session_revoked")
        return super().validate(attrs)
