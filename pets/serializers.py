from decimal import Decimal

from rest_framework import serializers

from listings.serializers import MAX_PHOTOS, AnimalFieldsSerializer, PartialAnimalFieldsMixin, PhotoSerializer
from pets.models import OWNER_REMOVE_REASONS, PetRemovedReason


class PetCreateSerializer(AnimalFieldsSerializer):
    """AQ8 · POST /me/pets. The fields that matter to the next owner travel with the Pet when it
    is rehomed (vet contact, medical notes, temperament...).
    Step 12 / PQ4 · every Feature Spec §3.2 field the `pet` table has: an approximate birthdate,
    weight, microchip number, feeding routine, allergies, medications and an emergency contact.
    Free-text care notes are capped at 1000 characters: a note, not a document."""
    color_markings = serializers.CharField(max_length=120, required=False, allow_blank=True)
    dob_approximate = serializers.BooleanField(required=False)
    weight_kg = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal("0.01"),
                                         required=False, allow_null=True)
    microchip_no = serializers.RegexField(r"^[A-Za-z0-9-]*$", max_length=40, required=False, allow_blank=True,
                                          error_messages={"invalid": "A microchip number has only letters, digits and dashes."})
    feeding_routine = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    allergies = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    medications = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    medical_conditions = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    vet_contact = serializers.CharField(max_length=160, required=False, allow_blank=True)
    emergency_contact = serializers.CharField(max_length=160, required=False, allow_blank=True)
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
