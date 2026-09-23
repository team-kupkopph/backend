"""P2 · how a shift is described, and to whom (review G1, decision D5).

Written once so the disclosure rule cannot drift between endpoints:
  public (anyone)                → what + when + city/province
  the approved volunteer         → + meeting point, street address, shelter contact
  the posting shelter            → + its own location, always
Never the volunteer's own address (D2).
"""
from shelter.models import ShelterProfile
from volunteer.models import SignupStatus


def shift_public(shift, approved_count=None):
    if approved_count is None:
        approved_count = shift.signups.filter(status=SignupStatus.APPROVED).count()
    return {"shift_id": str(shift.pk), "type": shift.type, "title": shift.title,
            "description": shift.description,
            "org_name": shift.shelter_account.display_name,
            "city": shift.city, "province": shift.province,
            "starts_at": shift.starts_at.isoformat(), "ends_at": shift.ends_at.isoformat(),
            "capacity": shift.capacity, "status": shift.status,
            "slots_left": max(shift.capacity - approved_count, 0)}


def shift_location(shift):
    return {"meeting_point": shift.meeting_point, "address_line1": shift.address_line1,
            "barangay": shift.barangay, "city": shift.city, "province": shift.province}


def shelter_contact(shift):
    profile = ShelterProfile.objects.filter(account_id=shift.shelter_account_id).first()
    if profile is None:
        return None
    return {"name": profile.contact_person_name, "phone": profile.official_phone,
            "email": profile.official_email}


def discloses_full(signup, now):
    """The approved-and-not-over rule, in one place."""
    return signup.status == SignupStatus.APPROVED and signup.shift.ends_at > now
