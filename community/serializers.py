from rest_framework import serializers

from .models import NeedCategory, NeedStatus, StoryType


class NeedCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=120)
    category = serializers.ChoiceField(choices=NeedCategory.choices)
    quantity_needed = serializers.IntegerField(min_value=1, default=1)
    description = serializers.CharField(required=False, allow_blank=True, default="")


class PledgeCreateSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, default=1)


class NeedPatchSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=120, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    quantity_needed = serializers.IntegerField(min_value=1, required=False)
    # The only client-driven status move is closing a need (open -> closed); fulfilled is
    # server-derived by the received-confirm writer, never set from the client.
    status = serializers.ChoiceField(choices=[NeedStatus.CLOSED], required=False)


class ReceivedSerializer(serializers.Serializer):
    pledge_id = serializers.UUIDField()
    quantity_received = serializers.IntegerField(min_value=1)


class StoryPhotoSerializer(serializers.Serializer):
    file_url = serializers.CharField()
    is_primary = serializers.BooleanField(required=False, default=False)


class StoryCreateSerializer(serializers.Serializer):
    caption = serializers.CharField(required=False, allow_blank=True, default="")
    story_type = serializers.ChoiceField(choices=StoryType.choices, required=False)
    photos = StoryPhotoSerializer(many=True, required=False, default=list)
    adoption_listing_id = serializers.UUIDField(required=False, allow_null=True)
    rescue_case_id = serializers.UUIDField(required=False, allow_null=True)
