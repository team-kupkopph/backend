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

v2 (T3): the document is assembled from `SECTIONS`, a name → builder registry that
`accounts/data_map.py` points at. Every table with an FK to `account` must either name a section
here or say why it is `Excluded`; a guard test enforces it, so a new table cannot be left out of
the export by accident. Shelter-only sections (`SHELTER_ONLY`) appear only in a shelter's export.
"""
from django.db.models import Count
from django.utils import timezone


def _dt(value):
    return value.isoformat() if value else None


EXPORT_FORMAT = "kupkop.export.v2"


# ------------------------------------------------------------------------------ sections
# Each builder takes the account and returns a JSON-serialisable value. Imports are local:
# every app depends on `accounts`, so importing them at module load risks an app-registry cycle.

def _account(account):
    return {
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
    }


def _settings(account):
    settings_row = account.settings
    return {
        "marketing_emails": settings_row.marketing_emails,
        "approximate_location": settings_row.approximate_location,
        "masked_contact": settings_row.masked_contact,
        "push_enabled": settings_row.push_enabled,
        "analytics_consent": settings_row.analytics_consent,
        "analytics_consent_at": _dt(settings_row.analytics_consent_at),
    }


def _addresses(account):
    from accounts.models import Address
    return [
        {"city": a.city, "barangay": a.barangay or None, "is_primary": a.is_primary}
        for a in Address.objects.filter(account=account)
    ]


def _identities(account):
    from accounts.models import AccountIdentity
    # Which sign-in methods are linked. The provider's own id for the person (`sub`) is not
    # exported: it is an identifier the provider minted, of no use to the person.
    return [
        {"provider": i.provider, "email": i.email or None, "linked_at": _dt(i.created_at)}
        for i in AccountIdentity.objects.filter(account=account).order_by("created_at")
    ]


def _pets(account):
    from listings.models import Pet
    return [
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
    ]


def _reports(account):
    from sagip.models import StrayReport
    # Their own reports, so the precise pin they dropped comes back to them (§12.5's
    # coarsening protects a report from STRANGERS; the reporter is not a stranger).
    return [
        {"report_id": str(r.report_id), "report_type": r.report_type,
         "species": r.species, "condition": r.condition, "status": r.status,
         "notes": r.notes or None, "city": r.city or None,
         "lat": r.geom.y if r.geom else None, "lng": r.geom.x if r.geom else None,
         "contact_shared": r.contact_share_consent,
         "created_at": _dt(r.created_at)}
        for r in StrayReport.objects.filter(reporter_account=account)
    ]


def _rescue_offers(account):
    from sagip.models import ReportOffer
    return [
        {"offer_id": str(o.offer_id), "report_id": str(o.report_id),
         "offer_type": o.offer_type, "status": o.status, "note": o.note or None,
         "contact_shared": o.contact_share_consent,
         "created_at": _dt(o.created_at)}
        for o in ReportOffer.objects.filter(account=account)
    ]


def _rescue_claims(account):
    from sagip.models import RescueCase
    return [
        {"case_id": str(c.case_id), "report_id": str(c.report_id),
         "claimed_at": _dt(c.claimed_at), "resolved_at": _dt(c.resolved_at),
         "contact_shared": c.contact_share_consent}
        for c in RescueCase.objects.filter(claimed_by_account=account)
    ]


def _listings(account):
    from listings.models import AdoptionListing
    return [
        {"listing_id": str(x.listing_id), "name": x.name, "species": x.species,
         "status": x.status, "city": x.city or None,
         "created_at": _dt(x.created_at)}
        for x in AdoptionListing.objects.filter(posted_by=account)
    ]


def _inquiries(account):
    from listings.models import AdoptionInquiry
    # SHARED ROW · the caller's side only. `listing__name` is the animal (public on the
    # browse anyway); the poster's identity and contact details are NOT exported.
    return [
        {"inquiry_id": str(i.inquiry_id), "listing_name": i.listing.name,
         "message": i.message or None, "status": i.status,
         "created_at": _dt(i.created_at)}
        for i in (AdoptionInquiry.objects.filter(adopter_account=account)
                  .select_related("listing"))
    ]


def _shortlist(account):
    from listings.models import ListingPreference
    # The Adopt deck's saved / hidden listings — a preference about the person, so it is
    # theirs to take. `listing__name` is the animal, public on the browse anyway.
    return [
        {"listing_id": str(p.listing_id), "listing_name": p.listing.name, "kind": p.kind,
         "created_at": _dt(p.created_at)}
        for p in (ListingPreference.objects.filter(account=account)
                  .select_related("listing").order_by("-updated_at"))
    ]


def _volunteer_signups(account):
    from volunteer.models import VolunteerSignup
    # SHARED ROW · the shift's time and the caller's own status. The host shelter's
    # contact details are shared per-shift under the §3.1.1 opt-in, not through here.
    return [
        {"signup_id": str(s.signup_id), "status": s.status,
         "starts_at": _dt(s.shift.starts_at), "type": s.shift.type,
         "waiver_accepted": s.waiver_accepted,
         "waiver_accepted_at": _dt(s.waiver_accepted_at)}
        for s in (VolunteerSignup.objects.filter(volunteer_account=account)
                  .select_related("shift"))
    ]


def _stories(account):
    from community.models import StoryPost
    return [
        {"story_id": str(s.story_id), "story_type": s.story_type,
         "caption": s.caption or None, "status": s.status,
         "created_at": _dt(s.created_at)}
        for s in StoryPost.objects.filter(author_account=account)
    ]


def _story_reactions(account):
    from community.models import StoryReaction
    return [
        {"story_id": str(r.story_id), "created_at": _dt(r.created_at)}
        for r in StoryReaction.objects.filter(account=account)
    ]


def _pledges(account):
    from community.models import NeedPledge
    # SHARED ROW · what the caller promised. The shelter's own records are theirs.
    return [
        {"pledge_id": str(p.pledge_id), "need_title": p.need.title,
         "quantity": p.quantity, "status": p.status, "note": p.note or None,
         "created_at": _dt(p.created_at)}
        for p in NeedPledge.objects.filter(pledger_account=account).select_related("need")
    ]


def _donation_qrs(account):
    from shelter.models import DonationQr
    # AT29 · a shelter's OWN donation QRs (live, pending, refused with the reviewer's reason, withdrawn): its payment targets are its data.
    return [
        {"donation_qr_id": str(q.donation_qr_id), "provider": q.provider, "account_name": q.account_name,
         "qr_image_url": q.qr_image_url, "status": q.status, "decision_note": q.decision_note or None,
         "created_at": _dt(q.created_at), "decided_at": _dt(q.decided_at)}
        for q in DonationQr.objects.filter(account=account).order_by("created_at")
    ]


def _badges(account):
    from community.models import AccountBadge
    return [
        {"badge_code": b.badge_id, "earned_at": _dt(b.earned_at)}
        for b in AccountBadge.objects.filter(account=account)
    ]


def _notifications(account):
    from notifications.models import Notification
    return [
        {"type": n.type, "title": n.title, "body": n.body,
         "read": n.read, "created_at": _dt(n.created_at)}
        for n in Notification.objects.filter(account=account)
    ]


def _capabilities(account):
    from verifications.models import AccountCapability
    return [
        {"capability": c.capability, "status": c.status, "granted_at": _dt(c.granted_at)}
        for c in AccountCapability.objects.filter(account=account)
    ]


def _verifications(account):
    from verifications.models import VerificationRequest
    # The person's own verification history. Left out on purpose: `notes` (the reviewer's
    # private notes), the reviewer's identity, and every document's FILE URL — those files live
    # in the restricted bucket and a link in a dump would be the one thing in it that outlives
    # the 90-day document purge. A document's `review_note` IS exported: it is the reason the
    # applicant was shown on the tracker.
    return [
        {"type": v.type, "status": v.status, "social_proof_url": v.social_proof_url or None,
         "submitted_at": _dt(v.submitted_at), "reviewed_at": _dt(v.reviewed_at),
         "consent_at": _dt(v.consent_at), "consent_version": v.consent_version or None,
         "documents": [
             {"doc_type": d.doc_type, "status": d.status, "review_note": d.review_note or None,
              "uploaded_at": _dt(d.uploaded_at), "purged_at": _dt(d.purged_at)}
             for d in v.documents.all().order_by("uploaded_at")
         ]}
        for v in (VerificationRequest.objects.filter(account=account)
                  .prefetch_related("documents").order_by("submitted_at"))
    ]


def _devices(account):
    from devices.models import DeviceToken
    # Which phones receive push. The FCM token itself is a bearer credential for sending to
    # that device, not data about the person, and is not exported.
    return [
        {"platform": d.platform, "registered_at": _dt(d.created_at),
         "last_used_at": _dt(d.last_used_at)}
        for d in DeviceToken.objects.filter(account=account)
    ]


def _moderation_flags(account):
    from moderation.models import ModerationFlag
    # Flags the caller RAISED, including the free text they typed. Not the flags raised
    # against their content: those are about them but authored by others.
    return [
        {"flag_id": str(f.flag_id), "target_type": f.target_type, "target_id": str(f.target_id),
         "reason": f.reason, "status": f.status, "created_at": _dt(f.created_at),
         "reviewed_at": _dt(f.reviewed_at)}
        for f in ModerationFlag.objects.filter(reporter_account=account)
    ]


# --------------------------------------------------------------------- shelter-only sections

def _shelter_profile(account):
    from shelter.models import ShelterProfile
    p = ShelterProfile.objects.filter(account=account).first()
    if p is None:
        return None
    return {
        "org_name": p.org_name, "org_type": p.org_type, "tier": p.tier,
        "description": p.description or None, "logo_url": p.logo_url or None,
        "registration_type": p.registration_type, "registration_number": p.registration_number or None,
        "contact_person_name": p.contact_person_name or None,
        "contact_person_role": p.contact_person_role or None,
        "official_email": p.official_email or None, "official_phone": p.official_phone or None,
        "website_url": p.website_url or None, "vet_name": p.vet_name or None,
        "vet_prc_number": p.vet_prc_number or None,
        "donation_instructions": p.donation_instructions or None,
    }


def _needs(account):
    from community.models import NeedPledge, ShelterNeed
    # The shelter's own wishlist. Who pledged is the DONORS' data, so a need carries only a
    # count of its pledges — never a name, an email or a note.
    counts = dict(NeedPledge.objects.filter(need__shelter_account=account)
                  .values_list("need_id").annotate(n=Count("pk")))
    return [
        {"need_id": str(n.need_id), "title": n.title, "category": n.category,
         "description": n.description or None, "quantity_needed": n.quantity_needed,
         "quantity_received": n.quantity_received, "unit": n.unit or None, "status": n.status,
         "pledges": counts.get(n.need_id, 0), "fulfilled_at": _dt(n.fulfilled_at),
         "created_at": _dt(n.created_at)}
        for n in ShelterNeed.objects.filter(shelter_account=account).order_by("created_at")
    ]


def _hosted_shifts(account):
    from volunteer.models import VolunteerShift, VolunteerSignup
    # The shelter's own shifts. Who signed up is the VOLUNTEERS' data (their contact details are
    # shared per-shift under the §3.1.1 opt-in), so a shift carries only counts by status.
    by_status = {}
    for shift_id, status, n in (VolunteerSignup.objects.filter(shift__shelter_account=account)
                                .values_list("shift_id", "status").annotate(n=Count("pk"))):
        by_status.setdefault(shift_id, {})[status] = n
    return [
        {"shift_id": str(s.shift_id), "type": s.type, "title": s.title or None,
         "description": s.description or None, "starts_at": _dt(s.starts_at),
         "ends_at": _dt(s.ends_at), "capacity": s.capacity, "status": s.status,
         "meeting_point": s.meeting_point or None, "address_line1": s.address_line1 or None,
         "barangay": s.barangay or None, "city": s.city or None, "province": s.province or None,
         "signups": by_status.get(s.shift_id, {}), "created_at": _dt(s.created_at)}
        for s in VolunteerShift.objects.filter(shelter_account=account).order_by("starts_at")
    ]


# Order is the order keys appear in the document. `accounts.data_map` points at these names.
SECTIONS = {
    "account": _account,
    "settings": _settings,
    "addresses": _addresses,
    "identities": _identities,
    "capabilities": _capabilities,
    "verifications": _verifications,
    "devices": _devices,
    "pets": _pets,
    "reports": _reports,
    "rescue_offers": _rescue_offers,
    "rescue_claims": _rescue_claims,
    "listings": _listings,
    "inquiries": _inquiries,
    "shortlist": _shortlist,
    "volunteer_signups": _volunteer_signups,
    "stories": _stories,
    "story_reactions": _story_reactions,
    "pledges": _pledges,
    "donation_qrs": _donation_qrs,
    "badges": _badges,
    "notifications": _notifications,
    "moderation_flags": _moderation_flags,
    "shelter_profile": _shelter_profile,
    "needs": _needs,
    "hosted_shifts": _hosted_shifts,
}

#: Present only in a shelter account's export; a personal account has none of these.
SHELTER_ONLY = frozenset({"shelter_profile", "needs", "hosted_shifts"})


def build_export(account):
    """The caller's data as one plain JSON-serialisable dict."""
    document = {
        # Provenance: a file with no date is impossible to reason about later.
        "exported_at": _dt(timezone.now()),
        "export_format": EXPORT_FORMAT,
    }
    for name, build in SECTIONS.items():
        if name in SHELTER_ONLY and account.account_type != "shelter":
            continue
        document[name] = build(account)
    return document


def export_filename(when=None):
    return f"kupkop-export-{(when or timezone.now()).date().isoformat()}.json"
