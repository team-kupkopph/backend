"""US-N3 · the RA 10173 data export (§12.6 portability, §12.7).

ONE RULE GOVERNS THIS WHOLE MODULE: **an export contains the caller's data and nobody
else's.** Every row here is either authored by, owned by, or addressed to the caller, and
shared rows (an inquiry, a pledge) contribute only the caller's side. Getting this wrong
turns a privacy feature into a data-leak endpoint, so each section spells out whose data it
is rather than reaching for a generic serializer that would happily follow a foreign key
into someone else's contact details.

Two deliberate omissions:
  * `password_hash` — portability is not a credential dump, and exporting a hash hands an
    attacker an offline cracking target for no benefit to the user.
  * Another party's identity on a shared row — an inquiry exports the caller's message and
    the animal, never the counterparty's email or phone.

§12.5's location rule is honoured by construction rather than by a filter: the only reports
in the document are ones the caller filed, and the precise pin they dropped themselves is
theirs to have back.
"""
from django.utils import timezone


def _dt(value):
    return value.isoformat() if value else None


def build_export(account):
    """The caller's data as one plain JSON-serialisable dict."""
    from accounts.models import Address
    from community.models import AccountBadge, NeedPledge, StoryPost, StoryReaction
    from listings.models import AdoptionInquiry, AdoptionListing, Pet
    from notifications.models import Notification
    from sagip.models import ReportOffer, RescueCase, StrayReport
    from volunteer.models import VolunteerSignup

    settings_row = account.settings

    document = {
        # Provenance: a file with no date is impossible to reason about later.
        "exported_at": _dt(timezone.now()),
        "export_format": "kupkop.export.v1",
        "account": {
            "account_id": str(account.account_id),
            "account_type": account.account_type,
            "email": account.email,
            "phone": account.phone,
            "display_name": account.display_name,
            "photo_url": account.photo_url or None,
            "email_verified_at": _dt(account.email_verified_at),
            "phone_verified_at": _dt(account.phone_verified_at),
            "terms_consent_at": _dt(account.terms_consent_at),
            "terms_consent_version": account.terms_consent_version or None,
            "status": account.status,
            "created_at": _dt(account.created_at),
            # password_hash is deliberately absent — see the module docstring.
        },
        "settings": {
            "marketing_emails": settings_row.marketing_emails,
            "approximate_location": settings_row.approximate_location,
            "masked_contact": settings_row.masked_contact,
            "push_enabled": settings_row.push_enabled,
            "analytics_consent": settings_row.analytics_consent,
            "analytics_consent_at": _dt(settings_row.analytics_consent_at),
        },
        "addresses": [
            {"city": a.city, "barangay": a.barangay or None, "is_primary": a.is_primary}
            for a in Address.objects.filter(account=account)
        ],
        "pets": [
            {"pet_id": str(p.pet_id), "name": p.name, "species": p.species,
             "breed": p.breed or None, "sex": p.sex or None,
             "date_of_birth": p.date_of_birth.isoformat() if p.date_of_birth else None,
             "color_markings": p.color_markings or None,
             "size_category": p.size_category or None,
             "spayed_neutered": p.spayed_neutered,
             "microchip_no": p.microchip_no or None,
             "feeding_routine": p.feeding_routine or None,
             "allergies": p.allergies or None, "medications": p.medications or None,
             "medical_conditions": p.medical_conditions or None,
             "temperament": p.temperament or None,
             "vet_contact": p.vet_contact or None,
             "emergency_contact": p.emergency_contact or None,
             "created_at": _dt(p.created_at)}
            for p in Pet.objects.filter(owner_account=account)
        ],
        # Their own reports, so the precise pin they dropped comes back to them (§12.5's
        # coarsening protects a report from STRANGERS; the reporter is not a stranger).
        "reports": [
            {"report_id": str(r.report_id), "report_type": r.report_type,
             "species": r.species, "condition": r.condition, "status": r.status,
             "notes": r.notes or None, "city": r.city or None,
             "lat": r.geom.y if r.geom else None, "lng": r.geom.x if r.geom else None,
             "created_at": _dt(r.created_at)}
            for r in StrayReport.objects.filter(reporter_account=account)
        ],
        "rescue_offers": [
            {"offer_id": str(o.offer_id), "report_id": str(o.report_id),
             "offer_type": o.offer_type, "status": o.status, "note": o.note or None,
             "created_at": _dt(o.created_at)}
            for o in ReportOffer.objects.filter(account=account)
        ],
        "rescue_claims": [
            {"case_id": str(c.case_id), "report_id": str(c.report_id),
             "claimed_at": _dt(c.claimed_at), "resolved_at": _dt(c.resolved_at)}
            for c in RescueCase.objects.filter(claimed_by_account=account)
        ],
        "listings": [
            {"listing_id": str(x.listing_id), "name": x.name, "species": x.species,
             "status": x.status, "city": x.city or None,
             "created_at": _dt(x.created_at)}
            for x in AdoptionListing.objects.filter(posted_by=account)
        ],
        # SHARED ROW · the caller's side only. `listing__name` is the animal (public on the
        # browse anyway); the poster's identity and contact details are NOT exported.
        "inquiries": [
            {"inquiry_id": str(i.inquiry_id), "listing_name": i.listing.name,
             "message": i.message or None, "status": i.status,
             "created_at": _dt(i.created_at)}
            for i in (AdoptionInquiry.objects.filter(adopter_account=account)
                      .select_related("listing"))
        ],
        # SHARED ROW · the shift's time and the caller's own status. The host shelter's
        # contact details are shared per-shift under the §3.1.1 opt-in, not through here.
        "volunteer_signups": [
            {"signup_id": str(s.signup_id), "status": s.status,
             "starts_at": _dt(s.shift.starts_at), "type": s.shift.type,
             "waiver_accepted": s.waiver_accepted,
             "waiver_accepted_at": _dt(s.waiver_accepted_at)}
            for s in (VolunteerSignup.objects.filter(volunteer_account=account)
                      .select_related("shift"))
        ],
        "stories": [
            {"story_id": str(s.story_id), "story_type": s.story_type,
             "caption": s.caption or None, "status": s.status,
             "created_at": _dt(s.created_at)}
            for s in StoryPost.objects.filter(author_account=account)
        ],
        "story_reactions": [
            {"story_id": str(r.story_id), "created_at": _dt(r.created_at)}
            for r in StoryReaction.objects.filter(account=account)
        ],
        # SHARED ROW · what the caller promised. The shelter's own records are theirs.
        "pledges": [
            {"pledge_id": str(p.pledge_id), "need_title": p.need.title,
             "quantity": p.quantity, "status": p.status,
             "created_at": _dt(p.created_at)}
            for p in NeedPledge.objects.filter(pledger_account=account).select_related("need")
        ],
        "badges": [
            {"badge_code": b.badge_id, "earned_at": _dt(b.earned_at)}
            for b in AccountBadge.objects.filter(account=account)
        ],
        "notifications": [
            {"type": n.type, "title": n.title, "body": n.body,
             "read": n.read, "created_at": _dt(n.created_at)}
            for n in Notification.objects.filter(account=account)
        ],
    }
    return document


def export_filename(when=None):
    return f"kupkop-export-{(when or timezone.now()).date().isoformat()}.json"
