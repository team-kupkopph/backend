"""How a pet reads, written once (My Pets review PP6): the list, the pet screen and the data export
all build on `pet_fields`. Moved from listings/representations.py in step 10."""
from listings.models import Sex


def _iso(dt):
    return dt.isoformat() if dt else None


# GET /me/pets/{id}, GET /me/pets and the data export used to map the fields by hand, three times,
# and had already drifted (the detail left out what the export had; the export left out photos).


def sex_out(sex):
    """PP16 · one way to say "unknown" in a response: null. A pet stores it as `unknown` (the DDL's
    default) and a listing as blank; no response shows either."""
    return None if sex in ("", Sex.UNKNOWN) else sex


def primary_photo(pet):
    """The flagged primary photo, else the earliest. Reads `pet.photos.all()` so a prefetch is used."""
    photos = sorted(pet.photos.all(), key=lambda ph: ph.uploaded_at)
    return next((ph for ph in photos if ph.is_primary), None) or (photos[0] if photos else None)


def pet_photo_urls(pet):
    """The pet's photos, primary first, then oldest first."""
    return [ph.url for ph in sorted(pet.photos.all(), key=lambda ph: (not ph.is_primary, ph.uploaded_at))]


def pet_summary(pet):
    """A My Pets row."""
    ph = primary_photo(pet)
    return {"pet_id": str(pet.pk), "name": pet.name, "species": pet.species,
            "photo_url": ph.url if ph else None}


def pet_fields(pet):
    """Everything the owner can know about their pet: every descriptive column, blanks as null."""
    return {
        "pet_id": str(pet.pk), "name": pet.name, "species": pet.species,
        "breed": pet.breed or None, "sex": sex_out(pet.sex),
        "birthdate": pet.date_of_birth.isoformat() if pet.date_of_birth else None,
        "dob_approximate": pet.dob_approximate,
        "color_markings": pet.color_markings or None,
        "weight_kg": str(pet.weight_kg) if pet.weight_kg is not None else None,
        "size_category": pet.size_category or None, "spayed_neutered": pet.spayed_neutered,
        "microchip_no": pet.microchip_no or None, "feeding_routine": pet.feeding_routine or None,
        "allergies": pet.allergies or None, "medications": pet.medications or None,
        "medical_conditions": pet.medical_conditions or None, "temperament": pet.temperament or None,
        "vet_contact": pet.vet_contact or None, "emergency_contact": pet.emergency_contact or None,
        "photos": pet_photo_urls(pet),
    }


def pet_ownership_history(pet, viewer):
    """PP18 · the pet's changes of hands, as its owner may see them: when, and whether each one
    was from or to them. Never the other person: their identity is their data, not the viewer's."""
    return [{"transferred_at": _iso(h.transferred_at),
             "from_you": h.from_account_id == viewer.pk, "to_you": h.to_account_id == viewer.pk}
            for h in sorted(pet.ownership_history.all(), key=lambda h: h.transferred_at)]


def rehomed_row(history):
    """PQ2 · a pet this account rehomed, as it was when it left (the snapshot on the history row)."""
    return {"name": history.pet_name, "photo_url": history.photo_url or None,
            "rehomed_at": _iso(history.transferred_at)}
