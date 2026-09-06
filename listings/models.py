import uuid

from django.contrib.gis.db import models  # GeoDjango: PointField + spatial manager

from accounts.models import Account
from sagip.models import Species, StrayReport


class Sex(models.TextChoices):
    MALE = "male"
    FEMALE = "female"
    UNKNOWN = "unknown"


class SizeCategory(models.TextChoices):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class ListingStatus(models.TextChoices):
    AVAILABLE = "available"
    PENDING = "pending"
    ADOPTED = "adopted"
    WITHDRAWN = "withdrawn"


class InquiryStatus(models.TextChoices):
    ACTIVE = "active"
    ADOPTED = "adopted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class AdoptionStageKey(models.TextChoices):
    INQUIRY = "inquiry"
    APPLICATION = "application"
    HOME_CHECK = "home_check"
    INTERVIEW = "interview"
    VET_CLEARANCE = "vet_clearance"
    FINALIZATION = "finalization"


class StageState(models.TextChoices):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"


# The six-stage ladder every inquiry gets, in order (US-A4 / decision 9). 'inquiry' starts
# DONE — submitting the inquiry is what completes that stage; the rest start NOT_STARTED.
ADOPTION_STAGE_ORDER = list(AdoptionStageKey)


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pet"


class PetPhoto(models.Model):
    photo_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pet = models.ForeignKey(Pet, on_delete=models.CASCADE, related_name="photos")
    url = models.TextField()
    is_primary = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pet_photo"


class AdoptionListing(models.Model):
    """Matches kupkop_mvp_schema.sql `adoption_listing`. The listing carries its OWN
    descriptive snapshot of the animal (name/species/breed/sex/…) rather than pointing
    at a `Pet` row — there is no owner yet, so no Pet can exist (see Pet's docstring).
    `adopted_pet` only gets populated once Track H's placement flow creates that Pet.

    ⚠️ `city` is NOT a DDL column — kept as a Sprint 1 convenience filter field, same
    precedent as `stray_report.city`: populated client-side from reverse-geocoding,
    never a substitute for `location_text`/`geom` (both DDL, both currently unpopulated —
    no listing endpoint collects precise location this sprint)."""

    listing_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    posted_by = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="listings",
                                  db_column="posted_by_account_id")
    source_report = models.ForeignKey(StrayReport, on_delete=models.SET_NULL, null=True,
                                      blank=True, related_name="adoption_listings")
    adopted_by_account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True,
                                           blank=True, related_name="+")
    adopted_pet = models.ForeignKey(Pet, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    name = models.CharField(max_length=80)
    species = models.CharField(max_length=10, choices=Species.choices)
    breed = models.CharField(max_length=80, blank=True)
    city = models.CharField(max_length=80)
    sex = models.CharField(max_length=10, choices=Sex.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    dob_approximate = models.BooleanField(default=False)
    size_category = models.CharField(max_length=10, choices=SizeCategory.choices, blank=True)
    spayed_neutered = models.BooleanField(null=True, blank=True)
    vaccinated = models.BooleanField(null=True, blank=True)
    walkable = models.BooleanField(default=False)
    temperament = models.CharField(max_length=160, blank=True)
    story = models.TextField(blank=True)
    adoption_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    requirements = models.TextField(blank=True)
    geom = models.PointField(geography=True, srid=4326, null=True, blank=True)
    location_text = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=20, choices=ListingStatus.choices,
                              default=ListingStatus.AVAILABLE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "adoption_listing"


class AdoptionListingPhoto(models.Model):
    photo_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    listing = models.ForeignKey(AdoptionListing, on_delete=models.CASCADE, related_name="photos")
    url = models.TextField()
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "adoption_listing_photo"


class AdoptionInquiry(models.Model):
    """One adopter's interest in one listing (matches `adoption_inquiry`).
    `UNIQUE(listing, adopter_account)` — one *active* inquiry record per pair; a second
    `POST .../inquiries` for the same pair is a 409, not a second row (US-A4)."""

    inquiry_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    listing = models.ForeignKey(AdoptionListing, on_delete=models.CASCADE, related_name="inquiries")
    adopter_account = models.ForeignKey(Account, on_delete=models.PROTECT,
                                        related_name="adoption_inquiries")
    message = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=InquiryStatus.choices,
                              default=InquiryStatus.ACTIVE)
    notes = models.TextField(blank=True)
    # Set when status leaves ACTIVE (adopted/declined/withdrawn). The consecutive-withdrawal
    # rule must order by decision time, not updated_at which is overwritten on any edit.
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "adoption_inquiry"
        constraints = [
            models.UniqueConstraint(fields=["listing", "adopter_account"],
                                    name="uq_adoption_inquiry_pair"),
        ]


class AdoptionStage(models.Model):
    """Current state of one stage in an inquiry's six-stage ladder (matches
    `adoption_stage`). Stages are flexible and skippable (decision 9) — `state` is the
    live snapshot; `AdoptionStageHistory` (below) is the append-only log of how it got
    there, written only by `listings.stages.set_stage_state`."""

    stage_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inquiry = models.ForeignKey(AdoptionInquiry, on_delete=models.CASCADE, related_name="stages")
    stage_key = models.CharField(max_length=20, choices=AdoptionStageKey.choices)
    state = models.CharField(max_length=20, choices=StageState.choices,
                             default=StageState.NOT_STARTED)
    note = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "adoption_stage"
        constraints = [
            models.UniqueConstraint(fields=["inquiry", "stage_key"], name="uq_adoption_stage"),
        ]


class AdoptionStageHistory(models.Model):
    """Append-only transition log for `AdoptionStage` (matches `adoption_stage_history`
    — added in the 2026-07-27 schema review specifically because "the core adoption
    funnel had no transition log — can't be backfilled"). Mirrors sagip's
    `CaseStatusHistory` pattern exactly: one writer, `listings.stages.set_stage_state`,
    so a stage can never move unlogged."""

    history_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inquiry = models.ForeignKey(AdoptionInquiry, on_delete=models.CASCADE,
                                related_name="stage_history")
    stage_key = models.CharField(max_length=20, choices=AdoptionStageKey.choices)
    state = models.CharField(max_length=20, choices=StageState.choices)
    changed_by_account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True,
                                           blank=True, related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "adoption_stage_history"
        indexes = [models.Index(fields=["inquiry", "-changed_at"], name="idx_adopt_stage_hist")]
