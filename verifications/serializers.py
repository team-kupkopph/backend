from rest_framework import serializers


class PresignSerializer(serializers.Serializer):
    # `purpose` stays a plain CharField (not ChoiceField) — an unknown purpose gets the
    # story's own documented `422 purpose_unknown`, not a generic field-validation 400.
    purpose = serializers.CharField()
    content_type = serializers.CharField()


class DocumentSerializer(serializers.Serializer):
    doc_type = serializers.CharField()
    file_url = serializers.CharField()


class SocialProofURLField(serializers.URLField):
    """F9 · the social link must be an http(s) URL — it is rendered to reviewers in the admin
    console, so `javascript:…` (and `ftp://…`, which URLField accepts by default) is refused."""

    default_error_messages = {"invalid": "Enter a link, e.g. https://facebook.com/yourpage"}

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value and not value.lower().startswith(("http://", "https://")):
            self.fail("invalid")
        return value


class VerificationSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=["rescuer", "shelter_org", "provider"])
    social_proof_url = SocialProofURLField(required=False, allow_blank=True)
    consent_version = serializers.CharField(required=False, allow_blank=True)
    bai_pending = serializers.BooleanField(required=False, default=False)   # US-C1: submit SEC now, BAI later
    documents = DocumentSerializer(many=True)
