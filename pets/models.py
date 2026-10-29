"""A person's own animals (My Pets): the pet, its photos, and its changes of hands.

Moved out of `listings` in My Pets review step 10 (PP8). The tables are the same ones (`pet`,
`pet_photo`, `pet_ownership_history`); only the Django app that owns them changed (see
pets/migrations/0001_initial.py). A pet is a profile asset (Feature Spec §3.2), not part of an
adoption listing; listings, sagip and accounts reach it through `pets.service`.
"""
import uuid

from django.contrib.gis.db import models

from accounts.models import Account
from listings.models import Sex, SizeCategory
from sagip.models import Species


class PetRemovedReason(models.TextChoices):
    """PQ1 · why a pet left its owner's My Pets without changing hands on Kupkop. The row is
    archived, never deleted, so lost reports, ownership history and badges keep a valid target."""
    REHOMED_OUTSIDE = "rehomed_outside"   # its rehoming listing was marked adopted outside Kupkop
    # PP12 · the owner removed it themselves (DELETE /me/pets/{id}).
    PASSED_AWAY = "passed_away"
    GIVEN_AWAY = "given_away"
    OTHER = "other"


#: The reasons an owner can give when removing a pet themselves.
OWNER_REMOVE_REASONS = (PetRemovedReason.PASSED_AWAY, PetRemovedReason.GIVEN_AWAY, PetRemovedReason.OTHER)


class PetQuerySet(models.QuerySet):
    def current(self):
        """The pets still in their owner's My Pets (not archived)."""
        return self.filter(removed_at__isnull=True)


class Pet(models.Model):
    """A person's OWNED animal (matches kupkop_mvp_schema.sql `pet`). `owner_account_id`
    is NOT NULL in the DDL, so a Pet row can only exist once someone actually owns the
    animal — that's Track H's US-H3 ("the animal joins My pets"), not this sprint.
    Modeled now so Track H is schedulable the moment its adoption-module dependency
    (this story) lands; nothing in Sprint 4's own endpoints creates a Pet row."""

    pet_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="pets")
    name = models.CharField(max_length=80)
    species = models.CharField(max_length=10, choices=Species.choices)
    breed = models.CharField(max_length=80, blank=True)
    sex = models.CharField(max_length=10, choices=Sex.choices, default=Sex.UNKNOWN)
    date_of_birth = models.DateField(null=True, blank=True)
    dob_approximate = models.BooleanField(default=False)
    color_markings = models.CharField(max_length=120, blank=True)
    weight_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    size_category = models.CharField(max_length=10, choices=SizeCategory.choices, blank=True)
    spayed_neutered = models.BooleanField(null=True, blank=True)
    microchip_no = models.CharField(max_length=40, blank=True)
    feeding_routine = models.TextField(blank=True)
    allergies = models.TextField(blank=True)
    medications = models.TextField(blank=True)
    medical_conditions = models.TextField(blank=True)
    temperament = models.CharField(max_length=160, blank=True)
    vet_contact = models.CharField(max_length=160, blank=True)
    emergency_contact = models.CharField(max_length=160, blank=True)
    # PQ1 · archived: gone from My Pets and from the lost-report pet picker, row kept.
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_reason = models.CharField(max_length=20, choices=PetRemovedReason.choices, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PetQuerySet.as_manager()

    class Meta:
        db_table = "pet"
        # PP15 · the rules the serializers keep, held by the database too. There is deliberately no
        # "birthdate not in the future" CHECK: it would need current_date, which Postgres treats as
        # non-immutable in a constraint (a dump can fail to restore). The serializer keeps that one.
        constraints = [
            models.CheckConstraint(condition=models.Q(name__regex=r"\S"), name="ck_pet_name_not_blank"),
            models.CheckConstraint(condition=models.Q(weight_kg__isnull=True) | models.Q(weight_kg__gt=0),
                                   name="ck_pet_weight_positive"),
        ]


class PetPhoto(models.Model):
    photo_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pet = models.ForeignKey(Pet, on_delete=models.CASCADE, related_name="photos")
    url = models.TextField()
    is_primary = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pet_photo"
        constraints = [   # PP15 · at most one primary photo per pet
            models.UniqueConstraint(fields=["pet"], condition=models.Q(is_primary=True),
                                    name="uq_pet_photo_one_primary"),
        ]


class PetOwnershipHistory(models.Model):
    """AQ8 · append-only: a Pet changed owners because a rehoming listing was completed. The new
    owner keeps the vet and medical fields on the Pet row; this records who it came from."""

    history_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pet = models.ForeignKey(Pet, on_delete=models.CASCADE, related_name="ownership_history")
    from_account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    to_account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    listing = models.ForeignKey("listings.AdoptionListing", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+")
    # PQ2 · what the previous owner's "Rehomed" trace shows, frozen at the transfer: reading the live
    # Pet would hand them the new owner's later edits (a new name, new photos).
    pet_name = models.CharField(max_length=80, blank=True)
    photo_url = models.TextField(blank=True)
    transferred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pet_ownership_history"
