from rest_framework import serializers

from listings.serializers import MAX_PHOTOS, AnimalFieldsSerializer, PartialAnimalFieldsMixin, PhotoSerializer
from pets.models import OWNER_REMOVE_REASONS, PetRemovedReason


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
