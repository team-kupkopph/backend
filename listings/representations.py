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


def poster_contact_phone(poster):
    """The number an adopter gets for a poster: a shelter's official phone, else the poster's own
    verified phone. None when there is neither. POST /screen refuses then, so a reveal is never
    one-sided."""
    profile = ShelterProfile.objects.filter(account=poster).first()
    if profile is not None and profile.official_phone:
        return profile.official_phone
    return poster.phone if poster.phone_verified_at else None


def _poster_contact(poster):
    profile = ShelterProfile.objects.filter(account=poster).first()
    name = (profile.contact_person_name or profile.org_name) if profile else poster.display_name
    return {"name": name, "phone": poster_contact_phone(poster)}


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


def adopter_inquiry_row(inquiry, verified_member=None):
    """The adopter's own inquiry. `verified_member` may be passed in by a list view that already
    knows it (it is the same account on every row)."""
    row = _base(inquiry, "adopter")
    row["verified_member"] = (account_is_verified_member(inquiry.adopter_account)
                              if verified_member is None else verified_member)
    if contact_revealed(inquiry):
        row["poster_contact"] = _poster_contact(inquiry.listing.posted_by)
    return row


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
        row["created_at"] = inquiry.created_at.isoformat()
        row["adopter"] = {"account_id": str(adopter.pk), "display_name": adopter.display_name,
                          "city": cities.get(adopter.pk), "verified_member": adopter.pk in members}
        if contact_revealed(inquiry):
            row["adopter_contact"] = {"name": adopter.display_name, "phone": adopter.phone}
        rows.append(row)
    return rows
