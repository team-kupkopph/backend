import re

from rest_framework import serializers

from common.phone import INVALID_PH_MOBILE_MESSAGE, normalize_ph_mobile
from shelter.models import OrgType, QrProvider, RegType, ShelterTier

PRC_RE = re.compile(r"^\d{6,8}$")   # US-C1: format check only; the register lookup is a reviewer action


class AddressSerializer(serializers.Serializer):
    line1 = serializers.CharField(required=False, allow_blank=True)
    barangay = serializers.CharField(required=False, allow_blank=True)
    city = serializers.CharField()
    province = serializers.CharField(required=False, allow_blank=True)


class ShelterProfileCreateSerializer(serializers.Serializer):
    org_name = serializers.CharField(max_length=150)
    org_type = serializers.ChoiceField(choices=OrgType.values)
    tier = serializers.ChoiceField(choices=ShelterTier.values)
    registration_type = serializers.ChoiceField(choices=RegType.values, required=False, allow_null=True)
    registration_number = serializers.CharField(max_length=60, required=False, allow_blank=True)
    logo_file_url = serializers.CharField(required=False, allow_blank=True)
    address = AddressSerializer()

    def validate(self, attrs):
        # A community rescue may have neither a registration type nor number; but a
        # number without its type (or vice-versa) is meaningless — require them together.
        if attrs.get("registration_type") and not attrs.get("registration_number"):
            raise serializers.ValidationError(
                {"registration_number": "Required when a registration type is set."})
        return attrs


class ShelterProfilePatchSerializer(serializers.Serializer):
    # US-B3 (contact) and US-C1 (vet) both PATCH this profile; accept either subset. T8 adds the
    # rest of what a shelter shows the world. `tier` and `org_type` are deliberately NOT here: a
    # tier change is an upgrade and re-runs verification (§3.5). The view needs `context["request"]`.
    org_name = serializers.CharField(max_length=150, required=False)      # locked once verified (view)
    description = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    logo_file_url = serializers.CharField(max_length=500, required=False, allow_blank=True)
    official_email = serializers.EmailField(max_length=254, required=False, allow_blank=True)
    operating_hours = serializers.CharField(max_length=120, required=False, allow_blank=True)
    contact_person_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    contact_person_role = serializers.CharField(max_length=60, required=False, allow_blank=True)
    official_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    website_url = serializers.CharField(required=False, allow_blank=True)
    vet_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    vet_prc_number = serializers.CharField(max_length=30, required=False, allow_blank=True)
    # US-W3 · the drop-off how-to shown on the wishlist; blank clears it.
    donation_instructions = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def validate_logo_file_url(self, value):
        # Rendered to donors, so only THIS account's own `shelter_logo` upload (common/uploads.py).
        if not value:
            return value        # blank clears it
        from common.uploads import own_upload_url_error
        error = own_upload_url_error(value, self.context["request"].user, "shelter_logo", noun="logo")
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate_official_phone(self, value):
        # F12 · same E.164 canonical form as /me/phone. Not unique — it may equal the
        # owner's own number — so only the format is enforced here; blank still clears.
        if not value:
            return value
        phone = normalize_ph_mobile(value)
        if phone is None:
            raise serializers.ValidationError(INVALID_PH_MOBILE_MESSAGE)
        return phone

    def validate_vet_prc_number(self, value):
        if value and not PRC_RE.match(value):
            raise serializers.ValidationError("PRC number must be 6–8 digits.")
        return value


class DonationQrUploadSerializer(serializers.Serializer):
    # US-Q1 · corrected against the DDL's actual donation_qr columns (provider/
    # qr_image_url), not the story's shorthand "{ file_url, channel }" — provider is the
    # DDL's real column name (gcash/maya), and every other file-carrying serializer in
    # this codebase (e.g. ReportCreateSerializer.photos) already calls it file_url.
    provider = serializers.ChoiceField(choices=QrProvider.values)
    account_name = serializers.CharField(max_length=120)
    file_url = serializers.CharField(max_length=500)
    # DW2 / owner decision Q3: replacing a LIVE QR needs the account password (a stolen session swapping the image is the fraud case).
    current_password = serializers.CharField(required=False, allow_blank=True, write_only=True)

    def validate_file_url(self, value):
        # AT10 · only this account's own donation_qr upload (see shelter/qr_rules.py).
        from shelter.qr_rules import donation_qr_url_error
        error = donation_qr_url_error(value, self.context["request"].user)
        if error:
            raise serializers.ValidationError(error)
        return value
