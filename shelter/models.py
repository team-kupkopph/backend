import uuid

from django.db import models

from accounts.models import Account


class ShelterTier(models.TextChoices):
    COMMUNITY_RESCUE = "community_rescue"      # tier 1 · Verified Rescue · fee cap ₱500
    REGISTERED_NGO = "registered_ngo"          # tier 2 · Verified Shelter · uncapped, escalation-eligible


class OrgType(models.TextChoices):
    SHELTER = "shelter"
    RESCUE = "rescue"
    POUND = "pound"


class QrProvider(models.TextChoices):
    GCASH = "gcash"
    MAYA = "maya"


class QrStatus(models.TextChoices):
    """DW2 · the life of one donation-QR row. A first upload or a replacement starts `pending`; the reviewer makes it `live` (a replacement
    withdraws the live row it replaces in the same transaction) or `refused`; the shelter or a reviewer can `withdraw` a live or pending one."""
    PENDING = "pending"
    LIVE = "live"
    REFUSED = "refused"
    WITHDRAWN = "withdrawn"


class RegType(models.TextChoices):
    SEC = "SEC"
    DTI = "DTI"
    BIR = "BIR"


class ShelterProfile(models.Model):
    """1:1 with account (matches kupkop_mvp_schema.sql `shelter_profile`). The `tier`
    column is the single source that drives the required document set (§3.5) and the
    trust badge — nothing about verification is stored as a boolean here; it is derived
    from a `verification_request(type='shelter_org', status='approved')`."""

    shelter_profile_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name="shelter_profile")
    org_name = models.CharField(max_length=150)
    org_type = models.CharField(max_length=20, choices=OrgType.choices)
    tier = models.CharField(max_length=20, choices=ShelterTier.choices,
                            default=ShelterTier.COMMUNITY_RESCUE)
    description = models.TextField(blank=True)
    logo_url = models.TextField(blank=True)
    registration_type = models.CharField(max_length=10, choices=RegType.choices, null=True, blank=True)
    registration_number = models.CharField(max_length=60, blank=True)
    contact_person_name = models.CharField(max_length=100, blank=True)
    contact_person_role = models.CharField(max_length=60, blank=True)
    official_email = models.CharField(max_length=254, blank=True)
    official_phone = models.CharField(max_length=20, blank=True)   # not unique — may equal the owner's
    website_url = models.TextField(blank=True)
    vet_name = models.CharField(max_length=120, blank=True)         # tier 2 only
    vet_prc_number = models.CharField(max_length=30, blank=True)    # tier 2; format-checked here, register-checked by a reviewer
    # T8 · "Mon-Sat 9am-5pm": shown to donors with the shelter's name, description and logo. Free text, not a schedule.
    operating_hours = models.CharField(max_length=120, blank=True, default="")
    is_escalation_partner = models.BooleanField(default=False)
    donation_instructions = models.CharField(max_length=500, blank=True, default="",
                                            db_default="")  # drop-off how-to shown on the wishlist

    class Meta:
        db_table = "shelter_profile"


class DonationQr(models.Model):
    """A donation QR for an account that receives donations (matches kupkop_mvp_schema.sql
    `donation_qr`). US-X3 adds only the reviewer-checked `verified` flag and its gate — the
    donations UI that lets a shelter upload a QR is Sprint 4. The public donate path is a
    two-key gate: the org must be approved AND the QR verified, so money can never route to an
    unchecked payment target even for an otherwise-approved org."""

    donation_qr_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="donation_qrs")
    provider = models.CharField(max_length=10, choices=QrProvider.choices)
    account_name = models.CharField(max_length=120)
    qr_image_url = models.TextField()
    # DW2 · `status` is authoritative. `verified` is DERIVED from it (`status == live`) and kept as a stored mirror for one release, so the
    # public gate, the dashboard and the console's `?verified=` filter keep working unchanged; it is dropped the release after.
    status = models.CharField(max_length=10, choices=QrStatus.choices, default=QrStatus.PENDING)
    verified = models.BooleanField(default=False)
    # The live row this pending one would replace (null for a first upload). Rows are immutable once decided: the audit trail a reviewer's
    # decision refers to.
    replaces = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="replacements")
    decision_note = models.TextField(blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(Account, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    def __init__(self, *args, **kwargs):
        # Legacy callers (tests, fixtures) create rows with `verified=True`; map that onto `status` so one rule holds.
        legacy = kwargs.get("verified")
        super().__init__(*args, **kwargs)
        if legacy is not None and "status" not in kwargs and not args:
            self.status = QrStatus.LIVE if legacy else QrStatus.PENDING

    def save(self, *args, **kwargs):
        self.verified = self.status == QrStatus.LIVE
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and ("status" in update_fields or "verified" in update_fields):
            kwargs["update_fields"] = list(set(update_fields) | {"status", "verified"})
        super().save(*args, **kwargs)

    class Meta:
        db_table = "donation_qr"
        indexes = [models.Index(fields=["account"], name="idx_donation_qr_account")]
        constraints = [
            # At most one live and one pending row per (account, provider): a replacement withdraws the previous pending one.
            models.UniqueConstraint(fields=["account", "provider"], condition=models.Q(status="live"), name="uq_donation_qr_one_live"),
            models.UniqueConstraint(fields=["account", "provider"], condition=models.Q(status="pending"), name="uq_donation_qr_one_pending"),
        ]
