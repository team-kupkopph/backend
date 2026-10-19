from decimal import Decimal

from rest_framework import serializers

from listings.models import Sex, SizeCategory, StageState
from sagip.models import Species

ZERO = Decimal("0")


MAX_PHOTOS = 6   # AD9 · what a listing's photo strip holds


class PhotoSerializer(serializers.Serializer):
    file_url = serializers.CharField()
    is_primary = serializers.BooleanField(required=False, default=False)


SIZES = [c.value for c in SizeCategory]


class PetFieldsSerializer(serializers.Serializer):
    """The `pet` object in US-A2's create body. These map onto AdoptionListing's own
    denormalized descriptive fields — not a `Pet` row (see listings/models.py::Pet's
    docstring: a Pet can't exist without an owner, and there is no owner yet)."""
    name = serializers.CharField(max_length=80)
    species = serializers.ChoiceField(choices=[c.value for c in Species])
    breed = serializers.CharField(max_length=80, required=False, allow_blank=True)
    sex = serializers.ChoiceField(choices=[c.value for c in Sex], required=False)
    birthdate = serializers.DateField(required=False, allow_null=True)


class ListingCreateSerializer(serializers.Serializer):
    pet = PetFieldsSerializer()
    pet_id = serializers.UUIDField(required=False)   # AQ8 · list one of the poster's own pets
    description = serializers.CharField(required=False, allow_blank=True)
    adoption_fee = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=ZERO)
    city = serializers.CharField(max_length=80)
    photos = PhotoSerializer(many=True, required=False)


class ListingPatchSerializer(serializers.Serializer):
    """Any subset (US-A2) — everything optional. Flat, not nested under `pet`, since a
    partial update naming only the fields that changed is simpler than requiring a
    full pet sub-object just to edit, say, the fee."""
    name = serializers.CharField(max_length=80, required=False)
    species = serializers.ChoiceField(choices=[c.value for c in Species], required=False)
    breed = serializers.CharField(max_length=80, required=False, allow_blank=True)
    sex = serializers.ChoiceField(choices=[c.value for c in Sex], required=False, allow_blank=True)
    birthdate = serializers.DateField(required=False, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True)
    adoption_fee = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=ZERO,
                                            required=False)
    city = serializers.CharField(max_length=80, required=False)


class InquiryCreateSerializer(serializers.Serializer):
    message = serializers.CharField(required=False, allow_blank=True)


class StageUpdateSerializer(serializers.Serializer):
    """⚠️ The story text's body shape (`status: "passed"|"failed"`) doesn't match the
    DDL's actual `stage_state` enum (not_started/in_progress/done/skipped) — there is no
    'passed'/'failed' value to write. Reconciled to the DDL's real states, same
    correction pattern as US-A2's `pet` insert claim. A poster rejecting the WHOLE
    inquiry (not just failing one stage) is a separate action this endpoint doesn't
    cover — see the story-file note."""
    state = serializers.ChoiceField(choices=[c.value for c in StageState if c != StageState.NOT_STARTED])
    note = serializers.CharField(required=False, allow_blank=True)


class PetCreateSerializer(serializers.Serializer):
    """AQ8 · POST /me/pets. The fields that matter to the next owner travel with the Pet when it
    is rehomed (vet contact, medical notes, temperament...)."""
    name = serializers.CharField(max_length=80)
    species = serializers.ChoiceField(choices=[c.value for c in Species])
    breed = serializers.CharField(max_length=80, required=False, allow_blank=True)
    sex = serializers.ChoiceField(choices=[c.value for c in Sex], required=False)
    birthdate = serializers.DateField(required=False, allow_null=True)
    color_markings = serializers.CharField(max_length=120, required=False, allow_blank=True)
    size_category = serializers.ChoiceField(choices=SIZES, required=False, allow_blank=True)
    spayed_neutered = serializers.BooleanField(required=False, allow_null=True)
    temperament = serializers.CharField(max_length=160, required=False, allow_blank=True)
    medical_conditions = serializers.CharField(required=False, allow_blank=True)
    vet_contact = serializers.CharField(max_length=160, required=False, allow_blank=True)
    photos = PhotoSerializer(many=True, required=False, max_length=MAX_PHOTOS)
