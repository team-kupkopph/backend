"""The read-only model browser's registry (US-V1).

⚠️ TWO ALLOW-LISTS, NOT A DENY-LIST — at the model level and the field level.

The obvious design is "expose every model, hide the sensitive fields". It fails OPEN twice
over: a model added next sprint is browsable before anyone has looked at it, and a column
added to an existing model is returned before anyone has decided it should be. The field
nobody thought to hide is the one that leaks.

Listing what MAY be shown fails closed instead. A model absent from this file is not
browsable at all; a field absent from its tuple is not returned. Adding either is a one-line,
reviewable act — which is the point.

The cost is real and is accepted: this file must be extended when a model should become
browsable. That is preferable to a browser that silently gains a `password_hash` column.
"""

# (app_label, ModelName): fields that may be READ.
#
# Deliberately absent everywhere: `password_hash` (accounts.Account), `fcm_token`
# (devices.DeviceToken), and `file_url` on verification_document — the last because a signed
# URL to a government ID must come from the review screen that logs the access, never from a
# generic table view.
SAFE_FIELDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("accounts", "Account"): (
        "account_id", "account_type", "email", "display_name", "status",
        "email_verified_at", "phone_verified_at", "created_at", "updated_at",
        "deleted_at", "anonymized_at", "last_active_at",
    ),
    ("accounts", "Address"): ("id", "account_id", "city", "province", "created_at"),
    ("shelter", "ShelterProfile"): (
        "shelter_profile_id", "account_id", "org_name", "org_type", "tier",
        "official_email", "official_phone", "website_url", "is_escalation_partner",
    ),
    ("shelter", "DonationQr"): (
        "donation_qr_id", "account_id", "provider", "account_name", "verified", "created_at",
    ),
    ("verifications", "VerificationRequest"): (
        "verification_id", "account_id", "type", "status", "submitted_at", "reviewed_at",
        "reviewed_by", "notes", "consent_at", "consent_version",
    ),
    ("verifications", "VerificationDocument"): (
        # No file_url. A signed URL to a gov ID belongs to the review screen, which logs it.
        "document_id", "verification_id", "doc_type", "status", "uploaded_at",
        "reviewed_at", "purged_at", "superseded_by",
    ),
    ("verifications", "AccountCapability"): (
        "capability_id", "account_id", "capability", "status", "granted_at",
    ),
    ("moderation", "ModerationFlag"): (
        "flag_id", "reporter_account_id", "target_type", "target_id", "reason",
        "status", "reviewed_by", "created_at", "reviewed_at",
    ),
    ("sagip", "StrayReport"): (
        "report_id", "reporter_account_id", "status", "city", "barangay",
        "description", "created_at",
    ),
    ("sagip", "RescueCase"): ("case_id", "report_id", "status", "created_at"),
    ("listings", "Pet"): ("pet_id", "name", "species", "sex", "created_at"),
    ("listings", "AdoptionListing"): (
        "listing_id", "posted_by", "pet_id", "listing_status", "fee", "city", "created_at",
    ),
    ("listings", "AdoptionInquiry"): (
        "inquiry_id", "listing_id", "inquirer_account_id", "status", "created_at",
    ),
    ("volunteer", "VolunteerShift"): (
        "shift_id", "shelter_account_id", "title", "starts_at", "ends_at", "capacity",
    ),
    ("volunteer", "VolunteerSignup"): (
        "signup_id", "shift_id", "volunteer_account_id", "status", "created_at", "cancelled_at",
    ),
    ("community", "StoryPost"): (
        "story_id", "author_account_id", "story_type", "status", "created_at",
    ),
    ("community", "ShelterNeed"): (
        "need_id", "shelter_account_id", "title", "status", "created_at",
    ),
    ("notifications", "Notification"): (
        "notification_id", "account_id", "type", "read_at", "created_at",
    ),
    ("devices", "DeviceToken"): (
        # No fcm_token — it is a credential for pushing to someone's phone.
        "id", "account_id", "platform", "created_at",
    ),
    ("adminapi", "AdminAuditLog"): (
        "audit_id", "actor_id", "actor_label", "action", "target_type", "target_id",
        "outcome", "created_at",
    ),
}

# Hard cap. No unbounded query reaches Postgres from this surface.
MAX_PAGE = 100


def registry():
    """Browsable models, derived from SAFE_FIELDS and resolved against the app registry."""
    from django.apps import apps
    out = []
    for (app_label, model_name), fields in sorted(SAFE_FIELDS.items()):
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            # A model that has been renamed or removed. Skipped rather than raising, so one
            # stale entry cannot take the whole browser down.
            continue
        out.append({
            "key": f"{app_label}.{model_name}",
            "app": app_label,
            "model": model_name,
            "verbose_name": str(model._meta.verbose_name),
            "field_count": len(fields),
        })
    return out


def resolve(key: str):
    """(model, safe_fields) for a browsable key, or (None, None)."""
    from django.apps import apps
    if not key or key.count(".") != 1:
        return None, None
    app_label, model_name = key.split(".")
    fields = SAFE_FIELDS.get((app_label, model_name))
    if fields is None:
        return None, None
    try:
        return apps.get_model(app_label, model_name), fields
    except LookupError:
        return None, None
