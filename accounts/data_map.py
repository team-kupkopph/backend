"""T3 · the data map — for every table that points at an `Account`, what an export says about it
and what the purge does to it. One registry, read by `accounts/export.py` and `accounts/purge.py`.

WHY THIS EXISTS. Export and erasure were each hand-written lists, and each drifted:

  * PR-6 (2026-10-27): a shelter's contact person, vet and official email were never scrubbed
    on deletion because nothing forced anyone to think about `ShelterProfile`;
  * X2: linked Google/Apple identities, notifications and pets survived the purge, so a
    returning Google user signed into the anonymised "Deleted user" row.

A new table with an FK to `account` used to be silently outside both. Now it cannot be: the
guard in `accounts/tests/test_data_map_guard.py` walks every model, finds every FK to `Account`
and fails on any that is not classified here. Adding a table is therefore one line in this file
plus the decision that line forces — export it or say why not; keep it or scrub it or delete it.

An entry is keyed by `("app.Model", "fk_field")` and carries:

  export   a section name in `accounts.export.SECTIONS` — the caller's own data — or
           `Excluded("reason")` when the row is not the subject's data to export (a staff
           reviewer's action, a counterparty's half of a shared row, a credential).
  erasure  what the purge does to rows pointing at the account:
             Delete()          remove them (a person's own preference or secret)
             Scrub({f: v})     keep the row, blank these fields (PII inside a welfare record)
             Keep("reason")    leave untouched — the reason is mandatory, because "keep" is the
                               decision that most needs defending (the welfare record, the audit
                               trail, a count that must not quietly drop)
"""
from dataclasses import dataclass, field

# Sections of the export that belong to no FK: the account row itself.
ROOT_SECTIONS = frozenset({"account"})


@dataclass(frozen=True)
class Excluded:
    reason: str


@dataclass(frozen=True)
class Delete:
    pass


@dataclass(frozen=True)
class Keep:
    reason: str


@dataclass(frozen=True)
class Scrub:
    values: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Entry:
    export: object          # str (a section in accounts.export.SECTIONS) or Excluded
    erasure: object         # Delete | Scrub | Keep


def account_foreign_keys():
    """Every `(model label, field name)` whose FK / one-to-one points at `Account`.

    Read from the app registry, not from a hand-kept list, so a new model is seen the moment it
    exists. Concrete forward relations only: the reverse side of the same FK would double-count.
    """
    from django.apps import apps

    from accounts.models import Account

    found = set()
    for model in apps.get_models():
        for f in model._meta.get_fields():
            if f.is_relation and f.concrete and (f.many_to_one or f.one_to_one) \
                    and f.related_model is Account:
                found.add((model._meta.label, f.name))
    return found


#: The `ShelterProfile` fields that are about people rather than the organisation (PR-6): a
#: contact person, a vet, whoever the drop-off note says to ask for. The org's own identity
#: (name, type, tier, registration, description, logo, website) stays — donors' records name
#: who they gave to by it.
SHELTER_PERSONAL_FIELDS = ("contact_person_name", "contact_person_role", "official_email",
                           "official_phone", "vet_name", "vet_prc_number", "donation_instructions")

_STAFF = Excluded("a staff reviewer's action, not the subject's data")
_AUDIT = Keep("audit trail of a staff action; erasing it would rewrite what the platform did")


def _entries():
    E = Entry
    return {
        # ------------------------------------------------------------------ accounts
        ("accounts.AccountIdentity", "account"):
            E("identities", Delete()),
        ("accounts.AccountSettings", "account"):
            E("settings", Keep("preferences with no PII; the row belongs to the (now anonymous) account")),
        ("accounts.Address", "account"):
            E("addresses", Delete()),
        ("accounts.StaffProfile", "account"):
            E(Excluded("staff bridge for a console login; staff are not app users"),
              Keep("links a console login to the account that stamps review decisions")),
        ("adminapi.AdminAuditLog", "actor"):
            E(_STAFF, _AUDIT),
        # ----------------------------------------------------------------- community
        ("community.AccountBadge", "account"):
            E("badges", Keep("a badge was earned; the welfare record of what the person did")),
        ("community.NeedPledge", "pledger_account"):
            E("pledges", Scrub({"note": ""})),      # R10 · the note may hold a phone number or a name
        ("community.ShelterNeed", "shelter_account"):
            E("needs", Keep("the shelter's wishlist; donors' pledges and receipts name it")),
        ("community.StoryPost", "author_account"):
            E("stories", Keep("published stories are hidden from the public feed once the author is "
                              "deleted, but stay as the record they are")),
        ("community.StoryReaction", "account"):
            E("story_reactions", Keep("keeps a story's reaction count from quietly dropping")),
        # ------------------------------------------------------------------- devices
        ("devices.DeviceToken", "account"):
            E("devices", Delete()),
        # ------------------------------------------------------------------ listings
        ("listings.AdoptionInquiry", "adopter_account"):
            E("inquiries", Keep("the adoption record; the animal's history is not the adopter's alone")),
        ("listings.AdoptionInquiry", "ended_by_account"):
            E(Excluded("the other party's action on a shared row; the caller's side is `inquiries`"),
              Keep("who ended an inquiry; part of the adoption record")),
        ("listings.AdoptionListing", "adopted_by_account"):
            E(Excluded("the listing is the poster's record; the adopter's side is `inquiries`"),
              Keep("who adopted the animal; the completed adoption is welfare record")),
        ("listings.AdoptionListing", "posted_by"):
            E("listings", Keep("a posted animal is a record; the public browse hides a deleted poster's listings")),
        ("listings.AdoptionStageHistory", "changed_by_account"):
            E(Excluded("an audit row of a stage change on a shared adoption"), _AUDIT),
        ("listings.ListingPreference", "account"):
            E("shortlist", Delete()),
        ("listings.Pet", "owner_account"):
            E("pets", Delete()),                    # AS2 · a privately owned pet is the owner's data
        ("listings.PetOwnershipHistory", "from_account"):
            E("rehomed_pets",                       # PQ2 · the pets the caller rehomed
              Keep("who the animal came from; goes with the pet if its owner is erased")),
        ("listings.PetOwnershipHistory", "to_account"):
            E(Excluded("exported with the pet, as `pets[].ownership_history` (PP18)"),
              Keep("who the animal went to; goes with the pet if its owner is erased")),
        # --------------------------------------------------------------- moderation
        ("moderation.ModerationFlag", "reporter_account"):
            # ⚠️ NULL here means "System" in the admin queue, so nulling a departed person's
            # flags would launder their reports into ones the platform raised itself.
            E("moderation_flags", Keep("NULL would mean 'System' in the admin queue; the audit trail "
                                       "must not be rewritten")),
        ("moderation.ModerationFlag", "reviewed_by"):
            E(_STAFF, _AUDIT),
        # ------------------------------------------------------------- notifications
        ("notifications.Notification", "account"):
            E("notifications", Delete()),
        # --------------------------------------------------------------------- sagip
        ("sagip.CaseStatusHistory", "changed_by_account"):
            E(Excluded("an audit row of a status change on a rescue case"), _AUDIT),
        ("sagip.ReportOffer", "account"):
            E("rescue_offers", Keep("an offer to help an animal; the rescue record")),
        ("sagip.RescueCase", "claimed_by_account"):
            E("rescue_claims", Keep("a rescue that happened still happened; the count of animals "
                                    "helped must not drop")),
        ("sagip.StrayReport", "reporter_account"):
            E("reports", Keep("the report is the animal's record; the reporter link is anonymised")),
        # ------------------------------------------------------------------- shelter
        ("shelter.DonationQr", "account"):
            # A payee name and a payment QR are financial identifiers; the row stays as the
            # reviewer's audit record (provider, status, decision) with both blanked.
            E("donation_qrs", Scrub({"account_name": "", "qr_image_url": ""})),
        ("shelter.DonationQr", "decided_by"):
            E(_STAFF, _AUDIT),
        ("shelter.ShelterProfile", "account"):
            E("shelter_profile", Scrub({f: "" for f in SHELTER_PERSONAL_FIELDS})),
        # -------------------------------------------------------------- verifications
        ("verifications.AccountCapability", "account"):
            E("capabilities", Keep("whether the account was approved as a rescuer; no PII")),
        ("verifications.VerificationAccessLog", "viewer"):
            E(_STAFF, Keep("who looked at an identity document; the access audit")),
        ("verifications.VerificationCode", "account"):
            E(Excluded("a one-time code is a credential, not data about the person"), Delete()),
        ("verifications.VerificationDocument", "reviewed_by"):
            E(_STAFF, _AUDIT),
        ("verifications.VerificationRequest", "account"):
            E("verifications", Keep("§12.6 keeps the decision record for the audit trail; the "
                                    "identity images are on their own 90-day clock")),
        ("verifications.VerificationRequest", "reviewed_by"):
            E(_STAFF, _AUDIT),
        # ----------------------------------------------------------------- volunteer
        ("volunteer.VolunteerShift", "shelter_account"):
            E("hosted_shifts", Keep("a shift that was hosted; volunteers' attendance record depends on it")),
        ("volunteer.VolunteerSignup", "volunteer_account"):
            E("volunteer_signups", Keep("shifts worked; the shelter's roster and the reliability "
                                        "signal depend on it")),
    }


DATA_MAP = _entries()
