from rest_framework import serializers

from sagip.models import OfferType, ReportType, Sex, SizeCategory, Species, StrayCondition, StrayStatus


class PhotoSerializer(serializers.Serializer):
    file_url = serializers.CharField()


class ReportCreateSerializer(serializers.Serializer):
    # US-L1 · report_type defaults to stray; lost/found reports additionally carry the four
    # describable fields the §11 matcher scores on (all optional — a report with none still
    # matches on proximity + time). A lost report may pass pet_id to prefill from My Pets.
    report_type = serializers.ChoiceField(choices=[c.value for c in ReportType],
                                          required=False, default=ReportType.STRAY)
    species = serializers.ChoiceField(choices=[c.value for c in Species])
    # D6 · optional for a LOST pet (its owner is describing a pet at home, not an animal in
    # front of them); required for a stray or a found animal, whose condition sets urgency.
    condition = serializers.ChoiceField(choices=[c.value for c in StrayCondition], required=False)
    # D6 · "I've seen this pet": the lost report this sighting is of. Makes this a FOUND report
    # of the lost pet's species, linked to it as a suggested match (sagip.matching.link_sighting).
    sighting_of = serializers.UUIDField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True)
    breed = serializers.CharField(required=False, allow_blank=True, max_length=80)
    color_markings = serializers.CharField(required=False, allow_blank=True, max_length=120)
    size_category = serializers.ChoiceField(choices=[c.value for c in SizeCategory],
                                            required=False, allow_blank=True)
    sex = serializers.ChoiceField(choices=[c.value for c in Sex], required=False,
                                  allow_blank=True)
    pet_id = serializers.UUIDField(required=False, allow_null=True)
    is_anonymous = serializers.BooleanField(required=False, default=False)
    # US-O3 · optional. Present only when the client queued this report offline; a report
    # filed online never needs one, so requiring it would break every existing caller.
    idempotency_key = serializers.CharField(required=False, allow_blank=False, max_length=64)
    # The one precise-GPS surface in the app (decision 11) — a real point, validated to
    # earth-plausible ranges. geom is NOT NULL, so both are required.
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lng = serializers.FloatField(min_value=-180, max_value=180)
    location_text = serializers.CharField(required=False, allow_blank=True, max_length=160)
    # City is a COARSE, city-level label (never the precise geom, §12.5). The client already
    # reverse-geocodes for `location_text`, so it passes the city it resolved rather than the
    # server standing up a geocoder (out of MVP scope). Blank/absent stays NULL — the map falls
    # back to the queried city, and report-detail simply omits it.
    city = serializers.CharField(required=False, allow_blank=True, max_length=80)
    photos = PhotoSerializer(many=True, required=False)
    # D1 · let whoever claims this contact me (phone + email). Off unless given.
    contact_share_consent = serializers.BooleanField(required=False, default=False)

    def validate(self, data):
        # D8 · anonymous means the claimer isn't told who reported — handing them a phone
        # number would undo that, so the two can't be combined.
        if not data.get("condition"):
            if data.get("report_type") == ReportType.LOST:
                data["condition"] = StrayCondition.HEALTHY
            else:
                raise serializers.ValidationError({"condition": "This field is required."})
        if data.get("is_anonymous") and data.get("contact_share_consent"):
            raise serializers.ValidationError(
                {"contact_share_consent": "An anonymous report can't share contact details."})
        return data


class CaseStatusUpdateSerializer(serializers.Serializer):
    """US-K2 · advance a claimed case. `claimed`/`reported` are never valid targets here —
    a case reaches this endpoint already claimed, so the only moves left are forward."""
    status = serializers.ChoiceField(
        choices=[StrayStatus.RESCUED, StrayStatus.SAFE, StrayStatus.RESOLVED])
    note = serializers.CharField(required=False, allow_blank=True, max_length=200)
    # Only meaningful (and only ever sent) alongside status="resolved" — the outcome screen.
    outcome_notes = serializers.CharField(required=False, allow_blank=True)
    outcome_photo_url = serializers.CharField(required=False, allow_blank=True)


class OfferCreateSerializer(serializers.Serializer):
    offer_type = serializers.ChoiceField(choices=[c.value for c in OfferType])
    # D1 · what the helper can do ("I have a car, free after 6pm") — the column always
    # existed, nothing could write it — and whether the claimer may contact them.
    note = serializers.CharField(required=False, allow_blank=True, max_length=200)
    contact_share_consent = serializers.BooleanField(required=False, default=False)


class ContactConsentSerializer(serializers.Serializer):
    """D1 · turn one person's contact sharing on or off for one rescue."""
    share = serializers.BooleanField()


class ReportCloseSerializer(serializers.Serializer):
    """S11 · why a reporter closed their own report. A fixed list, so the reason can be
    counted and shown back without free text anyone else could read."""
    # "reunited" (D6) is how an owner closes a lost report once their pet is home.
    reason = serializers.ChoiceField(
        choices=["gone", "duplicate", "handled_myself", "mistake", "reunited"])


class ClaimReleaseSerializer(serializers.Serializer):
    """D3 · why a claimer is letting go. A fixed list so the reason can be shown to the reporter
    and counted — sagip.notices.RELEASE_REASON_TEXT words each one."""
    reason = serializers.ChoiceField(
        choices=["cant_get_there", "cant_find", "no_capacity", "something_came_up"])
