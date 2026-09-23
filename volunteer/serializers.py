from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers
from rest_framework.response import Response

from volunteer.models import VolunteerType


class AwareDateTimeField(serializers.DateTimeField):
    """A datetime that must carry its own UTC offset.

    A shelter's "09:00" means 09:00 where the shelter is. Guessing that zone on the server is
    how K3 happened, so a value without an offset is refused rather than interpreted.
    """
    default_error_messages = {
        **serializers.DateTimeField.default_error_messages,
        "naive_datetime": "Include the time zone, e.g. 2026-10-04T09:00:00+08:00.",
    }

    def to_internal_value(self, value):
        if isinstance(value, str):
            raw = parse_datetime(value)
            if raw is not None and timezone.is_naive(raw):
                self.fail("naive_datetime")
        return super().to_internal_value(value)


class ShiftCreateSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=VolunteerType.values)
    starts_at = AwareDateTimeField()
    ends_at = AwareDateTimeField()
    capacity = serializers.IntegerField(min_value=1)


class ShiftPatchSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=VolunteerType.values, required=False)
    starts_at = AwareDateTimeField(required=False)
    ends_at = AwareDateTimeField(required=False)
    capacity = serializers.IntegerField(min_value=1, required=False)


class SignupCreateSerializer(serializers.Serializer):
    # Two independent consents. The waiver is required (checked in the view so it returns the
    # story's documented `422 waiver_required`, not a generic field error); contact-sharing is
    # optional and defaults to declined — a §12.5 exception must be opted INTO.
    waiver_accepted = serializers.BooleanField()
    contact_share_consent = serializers.BooleanField(required=False, default=False)


class AttendanceSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(choices=["completed", "no_show"])


def naive_datetime_response(serializer):
    """The one field error the volunteer endpoints name specifically: a time with no offset.
    Returns a 400 Response, or None when the serializer failed for some other reason."""
    for field, errors in serializer.errors.items():
        for err in errors:
            if getattr(err, "code", None) == "naive_datetime":
                return Response({"error": {"code": "naive_datetime", "message": str(err),
                                           "field": field}}, status=400)
    return None
