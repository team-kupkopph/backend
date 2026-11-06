from rest_framework import serializers

from moderation.models import FlagTarget, ReasonCode
from moderation.targets import VIA_TYPES

VIA_CHOICES = [t.value for t in VIA_TYPES]


class ModerationFlagCreateSerializer(serializers.Serializer):
    """`POST /moderation/flags`. A report names its target by id, or (for a PERSON) by what they posted: a story's
    author, a volunteer and a shelter are shown without an account id, so `via_type` + `via_id` name the thing and the
    server works out who. A category (`reason_code`) is enough on its own; `other` needs a note. An older app that
    sends only `reason` is `other`."""
    target_type = serializers.ChoiceField(choices=FlagTarget.values)
    target_id = serializers.UUIDField(required=False)
    via_type = serializers.ChoiceField(choices=VIA_CHOICES, required=False)
    via_id = serializers.UUIDField(required=False)
    reason_code = serializers.ChoiceField(choices=ReasonCode.values, required=False)
    reason = serializers.CharField(max_length=2000, required=False, allow_blank=True)

    def validate(self, attrs):
        via = (attrs.get("via_type"), attrs.get("via_id"))
        if any(via):
            if not all(via) or attrs["target_type"] != FlagTarget.ACCOUNT:
                raise serializers.ValidationError({"via_type": "Name a person by what they posted: via_type and via_id "
                                                               "together, with target_type account."})
            if "target_id" in attrs:
                raise serializers.ValidationError({"target_id": "Give a target_id or a via_type and via_id, not both."})
        elif "target_id" not in attrs:
            raise serializers.ValidationError({"target_id": "This field is required."})
        code = attrs.get("reason_code", ReasonCode.OTHER)
        note = (attrs.get("reason") or "").strip()
        if code == ReasonCode.OTHER and not note:
            raise serializers.ValidationError({"reason": "Tell us what's wrong."})
        attrs["reason_code"], attrs["reason"] = code, note
        return attrs


class BlockCreateSerializer(serializers.Serializer):
    """`POST /me/blocks`: a person by account id, or by what they posted (as for a report)."""
    account_id = serializers.UUIDField(required=False)
    via_type = serializers.ChoiceField(choices=VIA_CHOICES, required=False)
    via_id = serializers.UUIDField(required=False)

    def validate(self, attrs):
        by_via = bool(attrs.get("via_type") and attrs.get("via_id"))
        if ("account_id" in attrs) == by_via or (bool(attrs.get("via_type")) != bool(attrs.get("via_id"))):
            raise serializers.ValidationError({"account_id": "Name one person: an account_id, or a via_type and via_id."})
        return attrs
