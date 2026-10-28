from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from listings.models import OWNER_REMOVE_REASONS, PetRemovedReason, Sex, SizeCategory, StageState
from sagip.models import Species

ZERO = Decimal("0")


MAX_PHOTOS = 6   # AD9 · what a listing's photo strip holds


class PhotoSerializer(serializers.Serializer):
    file_url = serializers.CharField()
    is_primary = serializers.BooleanField(required=False, default=False)


SIZES = [c.value for c in SizeCategory]


class AnimalFieldsSerializer(serializers.Serializer):
    """PP7 · the fields that describe an animal, written once. A `Pet` (POST/PATCH /me/pets) and an
    adoption listing (the `pet{}` of POST /listings, and PATCH /listings/{id}) both describe the
    animal with these, so the two can't drift on a limit or a rule. A name of only spaces is
    trimmed to blank and refused; a birthdate can't be in the future (a typo, not an unborn pet)."""
    name = serializers.CharField(max_length=80)
    species = serializers.ChoiceField(choices=[c.value for c in Species])
    breed = serializers.CharField(max_length=80, required=False, allow_blank=True)
    sex = serializers.ChoiceField(choices=[c.value for c in Sex], required=False)
    birthdate = serializers.DateField(required=False, allow_null=True)
    # AD11 · what shelters list with, and what the detail's health tags read.
    size_category = serializers.ChoiceField(choices=SIZES, required=False, allow_blank=True)
    spayed_neutered = serializers.BooleanField(required=False, allow_null=True)
    temperament = serializers.CharField(max_length=160, required=False, allow_blank=True)

    def validate_birthdate(self, value):
        if value is not None and value > timezone.localdate():
            raise serializers.ValidationError("A birthdate can't be in the future.")
        return value


class PartialAnimalFieldsMixin(serializers.Serializer):
    """The same fields for a PATCH: nothing is required. Listed first in the bases so it wins."""
    name = serializers.CharField(max_length=80, required=False)
    species = serializers.ChoiceField(choices=[c.value for c in Species], required=False)


class PetFieldsSerializer(AnimalFieldsSerializer):
    """The `pet` object in US-A2's create body. These map onto AdoptionListing's own
    denormalized descriptive fields — not a `Pet` row (see listings/models.py::Pet's
    docstring: a Pet can't exist without an owner, and there is no owner yet)."""
    vaccinated = serializers.BooleanField(required=False, allow_null=True)
    walkable = serializers.BooleanField(required=False)


class ListingCreateSerializer(serializers.Serializer):
    pet = PetFieldsSerializer()
    pet_id = serializers.UUIDField(required=False)   # AQ8 · list one of the poster's own pets
    description = serializers.CharField(required=False, allow_blank=True)
    adoption_fee = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=ZERO)
    city = serializers.CharField(max_length=80)
    requirements = serializers.CharField(required=False, allow_blank=True)
    photos = PhotoSerializer(many=True, required=False, max_length=MAX_PHOTOS)


class ListingPatchSerializer(PartialAnimalFieldsMixin, AnimalFieldsSerializer):
    """Any subset (US-A2) — everything optional. Flat, not nested under `pet`, since a
    partial update naming only the fields that changed is simpler than requiring a
    full pet sub-object just to edit, say, the fee. The animal's fields are the shared ones."""
    sex = serializers.ChoiceField(choices=[c.value for c in Sex], required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    adoption_fee = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=ZERO,
                                            required=False)
    city = serializers.CharField(max_length=80, required=False)
    requirements = serializers.CharField(required=False, allow_blank=True)
    vaccinated = serializers.BooleanField(required=False, allow_null=True)
    walkable = serializers.BooleanField(required=False)
    # AD9 · present = the whole new set (the first flagged primary, else the first); absent = untouched.
    photos = PhotoSerializer(many=True, required=False, max_length=MAX_PHOTOS)


HOME_TYPES = ("house", "apartment", "condo", "other")


class ApplicationSerializer(serializers.Serializer):
    """AD8 · the adopter's quick answers. A closed set of keys: anything else is dropped, so the
    JSON column can't be used to stash arbitrary data on someone else's screen."""
    home_type = serializers.ChoiceField(choices=HOME_TYPES, required=False)
    rents = serializers.BooleanField(required=False)
    landlord_ok = serializers.BooleanField(required=False)
    other_pets = serializers.CharField(max_length=500, required=False, allow_blank=True)
    experience = serializers.CharField(max_length=500, required=False, allow_blank=True)
    schedule = serializers.CharField(max_length=500, required=False, allow_blank=True)


class InquiryCreateSerializer(serializers.Serializer):
    message = serializers.CharField(required=False, allow_blank=True)
    application = ApplicationSerializer(required=False)


class StageUpdateSerializer(serializers.Serializer):
    """⚠️ The story text's body shape (`status: "passed"|"failed"`) doesn't match the
    DDL's actual `stage_state` enum (not_started/in_progress/done/skipped) — there is no
    'passed'/'failed' value to write. Reconciled to the DDL's real states, same
    correction pattern as US-A2's `pet` insert claim. A poster rejecting the WHOLE
    inquiry (not just failing one stage) is a separate action this endpoint doesn't
    cover — see the story-file note."""
    state = serializers.ChoiceField(choices=[c.value for c in StageState if c != StageState.NOT_STARTED])
    note = serializers.CharField(required=False, allow_blank=True)


class PetCreateSerializer(AnimalFieldsSerializer):
    """AQ8 · POST /me/pets. The fields that matter to the next owner travel with the Pet when it
    is rehomed (vet contact, medical notes, temperament...)."""
    color_markings = serializers.CharField(max_length=120, required=False, allow_blank=True)
    medical_conditions = serializers.CharField(required=False, allow_blank=True)
    vet_contact = serializers.CharField(max_length=160, required=False, allow_blank=True)
    photos = PhotoSerializer(many=True, required=False, max_length=MAX_PHOTOS)

    def validate_photos(self, photos):
        """PP5 · a pet's photos are rendered to its next owner and deleted from the public bucket
        when its owner's account is purged, so each must be THIS account's own upload
        (common/uploads.py). The app uploads them under the `listing_photo` purpose."""
        from common.uploads import own_upload_url_error
        for photo in photos:
            error = own_upload_url_error(photo["file_url"], self.context["request"].user,
                                         "listing_photo", noun="photo")
            if error:
                raise serializers.ValidationError(error)
        return photos


class PetPatchSerializer(PartialAnimalFieldsMixin, PetCreateSerializer):
    """PP12 · PATCH /me/pets/{id}: any subset. `photos` present = the whole new set (the first flagged
    primary, else the first); absent = untouched. A photo the pet already holds is kept even when it
    isn't this account's upload: an adopted pet's photos are its previous poster's (PP5)."""

    def validate_photos(self, photos):
        held = set(self.context["pet"].photos.values_list("url", flat=True))
        super().validate_photos([p for p in photos if p["file_url"] not in held])
        return photos


class PetRemoveSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(choices=[r.value for r in OWNER_REMOVE_REASONS], required=False,
                                     default=PetRemovedReason.OTHER.value)
