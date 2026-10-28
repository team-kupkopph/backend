"""How an inquiry reads to each side of it (AD1/AD2). Two tiers, never merged:

* the ADOPTER's row: their own inquiry, the ladder, and whether they hold the badge Reserve
  will need (AQ2); plus the poster's contact once revealed;
* the POSTER's row: who asked, what they wrote, the ladder; plus the adopter's phone once
  revealed.

The reveal rule lives in one function (`contact_revealed`), so the two tiers can't disagree about
whether the exchange has happened (AQ1)."""
from accounts.models import Address
from listings.models import AdoptionStageKey, InquiryStatus, StageState
from listings.visibility import account_is_verified_member
from shelter.models import ShelterProfile
from verifications.models import AccountCapability


def _iso(dt):
    return dt.isoformat() if dt else None


def stage_json(key, stage):
    # All six rows exist from the inquiry's first second, so a row's `updated_at` is only a date
    # the ladder should show once the stage has MOVED. `note` is null when empty for the same reason.
    if stage is None or stage.state == StageState.NOT_STARTED:
        return {"stage_key": key, "state": StageState.NOT_STARTED, "updated_at": None, "note": None}
    return {"stage_key": key, "state": stage.state,
            "updated_at": stage.updated_at.isoformat(), "note": stage.note or None}


def contact_revealed(inquiry):
    """AQ1 · both phones are shared once the poster accepts the applicant for screening, for as
    long as the inquiry is open or ended in an adoption. A declined or withdrawn applicant loses
    the other side's number."""
    return (inquiry.accepted_at is not None
            and inquiry.status in (InquiryStatus.ACTIVE, InquiryStatus.ADOPTED))


def _phone_for(poster, profile):
    if profile is not None and profile.official_phone:
        return profile.official_phone
    return poster.phone if poster.phone_verified_at else None


def poster_contact_phone(poster):
    """The number an adopter gets for a poster: a shelter's official phone, else the poster's own
    verified phone. None when there is neither. POST /screen refuses then, so a reveal is never
    one-sided."""
    return _phone_for(poster, ShelterProfile.objects.filter(account=poster).first())


def _poster_contact(poster, profile):
    name = (profile.contact_person_name or profile.org_name) if profile else poster.display_name
    return {"name": name, "phone": _phone_for(poster, profile)}


def _base(inquiry, viewer):
    by_key = {s.stage_key: s for s in inquiry.stages.all()}
    return {
        "viewer": viewer,
        "inquiry_id": str(inquiry.pk), "kind": inquiry.kind, "status": inquiry.status,
        "listing": {"listing_id": str(inquiry.listing_id), "name": inquiry.listing.name,
                    "species": inquiry.listing.species, "status": inquiry.listing.status},
        "accepted_at": _iso(inquiry.accepted_at), "reserved_at": _iso(inquiry.reserved_at),
        "end_reason": inquiry.end_reason or None,
        "stages": [stage_json(key, by_key.get(key)) for key in AdoptionStageKey],
    }


def adopter_inquiry_row(inquiry, verified_member=None, profiles=None):
    """The adopter's own inquiry. A list view passes what it already knows for the whole page:
    `verified_member` (the same account on every row) and `profiles` (poster account id ->
    ShelterProfile, see adopter_inquiry_rows). Left out, they are looked up for this one row."""
    row = _base(inquiry, "adopter")
    row["verified_member"] = (account_is_verified_member(inquiry.adopter_account)
                              if verified_member is None else verified_member)
    if contact_revealed(inquiry):
        poster = inquiry.listing.posted_by
        profile = (ShelterProfile.objects.filter(account=poster).first() if profiles is None
                   else profiles.get(poster.pk))
        row["poster_contact"] = _poster_contact(poster, profile)
    return row


def adopter_inquiry_rows(inquiries, verified_member=None):
    """A page of the adopter's own inquiries: one query for every revealed poster's shelter
    profile, not one per row."""
    posters = {i.listing.posted_by_id for i in inquiries if contact_revealed(i)}
    profiles = {p.account_id: p for p in ShelterProfile.objects.filter(account_id__in=posters)}
    return [adopter_inquiry_row(i, verified_member=verified_member, profiles=profiles)
            for i in inquiries]


def poster_inquiry_rows(inquiries):
    """The poster's view of a page of inquiries: two queries for the adopters' cities and badges,
    not two per row."""
    ids = {i.adopter_account_id for i in inquiries}
    cities = dict(Address.objects.filter(account_id__in=ids, is_primary=True)
                  .values_list("account_id", "city"))
    members = set(AccountCapability.objects.filter(account_id__in=ids, capability="rescuer",
                                                   status="approved")
                  .values_list("account_id", flat=True))
    rows = []
    for inquiry in inquiries:
        adopter = inquiry.adopter_account
        row = _base(inquiry, "poster")
        row["message"] = inquiry.message or None
        row["application"] = inquiry.application or None      # AD8
        row["created_at"] = inquiry.created_at.isoformat()
        row["adopter"] = {"account_id": str(adopter.pk), "display_name": adopter.display_name,
                          "city": cities.get(adopter.pk), "verified_member": adopter.pk in members}
        if contact_revealed(inquiry):
            # AQ1 · the adopter's *verified* phone: a phone change clears phone_verified_at.
            row["adopter_contact"] = {"name": adopter.display_name,
                                      "phone": adopter.phone if adopter.phone_verified_at else None}
        rows.append(row)
    return rows


# ── Pets (My Pets review PP6) ───────────────────────────────────────────────────────────────
#
# How a pet reads, written once. GET /me/pets/{id}, GET /me/pets and the data export used to map
# the fields by hand, three times, and had already drifted (the detail left out what the export
# had; the export left out photos). Every caller below builds on `pet_fields`.

def sex_out(sex):
    """PP16 · one way to say "unknown" in a response: null. A pet stores it as `unknown` (the DDL's
    default) and a listing as blank; no response shows either."""
    from listings.models import Sex
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
