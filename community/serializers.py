from rest_framework import serializers
from rest_framework.exceptions import APIException

from .models import NeedCategory, NeedStatus, StoryType
from .needs import MAX_QTY


class BadCategory(APIException):
    """`funds` stays a valid enum value for rows that already have it, but no new or edited
    need may take it (a wishlist asks for things, not money). Raised as its own exception —
    a plain `ValidationError` would reach the envelope as the generic `invalid` code."""
    status_code = 400
    default_code = "bad_category"


def _refuse_funds(value):
    if value == NeedCategory.FUNDS:
        raise BadCategory({"category": ["A wishlist can't ask for funds."]})
    return value


class NeedCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=120)
    category = serializers.ChoiceField(choices=NeedCategory.choices)
    quantity_needed = serializers.IntegerField(min_value=1, max_value=MAX_QTY, default=1)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    unit = serializers.CharField(max_length=16, required=False, allow_blank=True, default="")

    def validate_category(self, value):
        return _refuse_funds(value)


class PledgeCreateSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, max_value=MAX_QTY, default=1)
    # R2 · for the owning shelter's eyes only; no public read and not /me/pledges returns it.
    note = serializers.CharField(max_length=280, required=False, allow_blank=True, default="")


class NeedPatchSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=120, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    quantity_needed = serializers.IntegerField(min_value=1, max_value=MAX_QTY, required=False)
    category = serializers.ChoiceField(choices=NeedCategory.choices, required=False)
    unit = serializers.CharField(max_length=16, required=False, allow_blank=True)
    # The only client-driven status move is closing a need (open -> closed); fulfilled is
    # server-derived by the received-confirm writer, never set from the client.
    status = serializers.ChoiceField(choices=[NeedStatus.CLOSED], required=False)

    def validate_category(self, value):
        return _refuse_funds(value)


class ReceivedSerializer(serializers.Serializer):
    pledge_id = serializers.UUIDField()
    quantity_received = serializers.IntegerField(min_value=1, max_value=MAX_QTY)


class StoryPhotoSerializer(serializers.Serializer):
    file_url = serializers.CharField()
    is_primary = serializers.BooleanField(required=False, default=False)


class StoryCreateSerializer(serializers.Serializer):
    caption = serializers.CharField(required=False, allow_blank=True, default="")
    story_type = serializers.ChoiceField(choices=StoryType.choices, required=False)
    photos = StoryPhotoSerializer(many=True, required=False, default=list)
    adoption_listing_id = serializers.UUIDField(required=False, allow_null=True)
    rescue_case_id = serializers.UUIDField(required=False, allow_null=True)
