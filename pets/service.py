"""The one place that writes a pet (My Pets review step 10, PP8).

Every change to a `Pet` row goes through here: an owner adding, editing or removing one, a pet
changing hands at the end of an adoption, a rehoming that ended outside Kupkop. `listings`,
`sagip` and `accounts` call these functions and never write the pet tables themselves, so a rule
about pets lives in one module.

Locking. Every writer locks the pet row. A writer that also touches an adoption listing locks the
listing FIRST and the pet after (R1's order, extended: case -> report -> listing -> inquiry ->
pet), except `remove_pet`, which locks only the pet and then READS the listing: a listing created
at the same moment locks the pet too (`find_own_pet(..., lock=True)`), so either it is seen or it
waits.
"""
from django.db import transaction
from django.utils import timezone

from listings.models import OPEN_REHOME_STATUSES, AdoptionListing, Sex
from pets.models import Pet, PetOwnershipHistory, PetPhoto, PetRemovedReason
from pets.representations import primary_photo
from sagip.models import ReportType, StrayReport, StrayStatus


class PetRefused(Exception):
    """A pet write the rules refuse. The view turns it into the {"error": ...} envelope."""

    def __init__(self, code, message, status, details=None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details


def no_such_pet():
    return PetRefused("not_found", "No such pet", 404)


# ── Reads ───────────────────────────────────────────────────────────────────────────────────

def find_own_pet(owner, pet_id, lock=False):
    """The owner's CURRENT pet with this id, or None. Another account's pet and an archived one
    are both None: neither is the caller's to see or change."""
    qs = Pet.objects.current().filter(pk=pet_id, owner_account=owner)
    return (qs.select_for_update() if lock else qs).first()


def open_rehome_listing(pet):
    return (AdoptionListing.objects.filter(rehome_pet=pet, status__in=OPEN_REHOME_STATUSES)
            .only("pk", "status").first())


def pet_states(pets):
    """PP14 · what each pet is in the middle of, for its card and its screen: its open rehoming
    listing (with whether moderation removed it) and its open lost report. Two queries for any
    number of pets. A lost report is open until it is resolved."""
    ids = [p.pk for p in pets]
    listings = {row["rehome_pet_id"]: {"listing_id": str(row["pk"]), "status": row["status"],
                                       "removed": row["hidden_at"] is not None}
                for row in AdoptionListing.objects.filter(rehome_pet_id__in=ids, status__in=OPEN_REHOME_STATUSES)
                .values("rehome_pet_id", "pk", "status", "hidden_at")}
    lost = {}
    for row in (StrayReport.objects.filter(pet_id__in=ids, report_type=ReportType.LOST)
                .exclude(status=StrayStatus.RESOLVED).order_by("created_at").values("pet_id", "pk")):
        lost.setdefault(row["pet_id"], str(row["pk"]))
    return {pk: {"open_listing": listings.get(pk), "lost_report_id": lost.get(pk)} for pk in ids}


def rehomed_by(account):
    """PQ2 · the transfers this account handed a pet over in, newest first."""
    return (PetOwnershipHistory.objects.filter(from_account=account).exclude(to_account=account)
            .order_by("-transferred_at"))


def fill_from_pet(fields, pet):
    """PP3 · a rehoming listing's `pet{}` with every blank taken from the owner's Pet. What the
    owner did type wins (the listing may word things differently); the species is checked by the
    caller, never filled. A pet's "unknown" sex stays blank on the listing, as listings write it."""
    filled = dict(fields)
    for key, value in (("breed", pet.breed), ("birthdate", pet.date_of_birth),
                       ("size_category", pet.size_category), ("temperament", pet.temperament),
                       ("sex", "" if pet.sex == Sex.UNKNOWN else pet.sex)):
        if not filled.get(key):
            filled[key] = value
    if filled.get("spayed_neutered") is None:
        filled["spayed_neutered"] = pet.spayed_neutered
    return filled


# ── An owner's own writes ───────────────────────────────────────────────────────────────────

#: PATCH body key -> Pet column, for the fields an owner edits as typed.
_EDITABLE = {"name": "name", "species": "species", "breed": "breed", "sex": "sex",
             "birthdate": "date_of_birth", "color_markings": "color_markings",
             "size_category": "size_category", "spayed_neutered": "spayed_neutered",
             "temperament": "temperament", "medical_conditions": "medical_conditions",
             "vet_contact": "vet_contact"}


def _set_photos(pet, photos):
    """Replace the pet's photo set: the first flagged primary is the primary, else the first."""
    pet.photos.all().delete()
    flagged = next((i for i, p in enumerate(photos) if p.get("is_primary")), 0)
    for i, photo in enumerate(photos):
        PetPhoto.objects.create(pet=pet, url=photo["file_url"], is_primary=(i == flagged))


def create_pet(owner, data):
    """AQ8 · POST /me/pets. `data` is a validated PetCreateSerializer. PQ3 · not for a shelter: its
    animals are listings (D7)."""
    if owner.account_type == "shelter":
        raise PetRefused("not_for_shelters", "A shelter's animals are its listings", 403)
    with transaction.atomic():
        pet = Pet.objects.create(
            owner_account=owner, name=data["name"], species=data["species"],
            breed=data.get("breed", ""), sex=data.get("sex", Sex.UNKNOWN),
            date_of_birth=data.get("birthdate"), color_markings=data.get("color_markings", ""),
            size_category=data.get("size_category", ""), spayed_neutered=data.get("spayed_neutered"),
            temperament=data.get("temperament", ""),
            medical_conditions=data.get("medical_conditions", ""),
            vet_contact=data.get("vet_contact", ""))
        _set_photos(pet, data.get("photos", []))
    return pet


def update_pet(owner, pet_id, validate):
    """PP12 · PATCH /me/pets/{id}. `validate(pet)` returns the validated body for THIS pet (the
    photo rule depends on which photos it already holds) and runs under the lock. The species
    can't change while an open listing describes the animal (PP3)."""
    with transaction.atomic():
        pet = find_own_pet(owner, pet_id, lock=True)
        if pet is None:
            raise no_such_pet()
        data = validate(pet)
        if "species" in data and data["species"] != pet.species and open_rehome_listing(pet):
            raise PetRefused("pet_listed", "Withdraw this pet's listing to change the animal", 409)
        changed = [column for key, column in _EDITABLE.items() if key in data]
        for key, column in _EDITABLE.items():
            if key in data:
                setattr(pet, column, data[key])
        pet.save(update_fields=[*changed, "updated_at"])
        if "photos" in data:
            _set_photos(pet, data["photos"])
    return pet


def remove_pet(owner, pet_id, reason):
    """PP12 / PQ1 · DELETE /me/pets/{id}: archive with the owner's reason, never while an open
    rehoming listing exists. The row is kept (lost reports, history and badges point at it)."""
    with transaction.atomic():
        pet = find_own_pet(owner, pet_id, lock=True)
        if pet is None:
            raise no_such_pet()
        listing = open_rehome_listing(pet)
        if listing is not None:
            raise PetRefused("pet_listed", "Withdraw this pet's listing first", 409,
                             {"listing_id": str(listing.pk)})
        pet.removed_at = timezone.now()
        pet.removed_reason = reason
        pet.save(update_fields=["removed_at", "removed_reason", "updated_at"])
    return pet


# ── A pet changing hands (called by listings, inside its own transaction) ───────────────────

def transfer_pet(pet, to_account, listing):
    """AQ8 · hand a locked pet to its new owner, keeping its record (vet, medical notes) and
    writing the history row with the PQ2 snapshot: the name and photo as they were when it left."""
    ph = primary_photo(pet)
    PetOwnershipHistory.objects.create(pet=pet, from_account=pet.owner_account, to_account=to_account,
                                       listing=listing, pet_name=pet.name, photo_url=ph.url if ph else "")
    pet.owner_account = to_account
    pet.save(update_fields=["owner_account", "updated_at"])
    return pet


def pet_from_listing(listing, owner):
    """US-H3 / AQ3 / AQ8 · the adopter's Pet for an animal they just took home. A listing made from
    one of the poster's own pets hands that very Pet over; any other listing gets a fresh Pet
    carrying the listing's description of the animal and its photos (primary kept). Shared by a
    placement Accept and a public Complete. The caller holds the listing's lock (R1)."""
    if listing.rehome_pet_id:
        pet = Pet.objects.select_for_update().filter(pk=listing.rehome_pet_id).first()
        if pet is not None:
            return transfer_pet(pet, owner, listing)
    # PP4 · everything the listing knew about the animal, so the adopter's My Pets isn't a bare name.
    pet = Pet.objects.create(
        owner_account=owner, name=listing.name or "Pet", species=listing.species,
        breed=listing.breed, sex=listing.sex or Sex.UNKNOWN, date_of_birth=listing.date_of_birth,
        dob_approximate=listing.dob_approximate, size_category=listing.size_category,
        spayed_neutered=listing.spayed_neutered, temperament=listing.temperament)
    for ph in listing.photos.all():
        PetPhoto.objects.create(pet=pet, url=ph.url, is_primary=ph.is_primary)
    return pet


def archive_rehomed_outside(listing, now):
    """PP2 / PQ1 · a rehoming listing marked adopted outside Kupkop: its pet has left its owner.
    Archived, never deleted. The caller holds the listing's lock; the pet is locked after it."""
    if listing.rehome_pet_id is None:
        return
    Pet.objects.select_for_update().filter(
        pk=listing.rehome_pet_id, owner_account=listing.posted_by_id, removed_at__isnull=True,
    ).update(removed_at=now, removed_reason=PetRemovedReason.REHOMED_OUTSIDE, updated_at=now)
