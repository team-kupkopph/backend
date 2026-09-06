import uuid

from django.db import models

from accounts.models import Account


class OtpChannel(models.TextChoices):
    EMAIL = "email"
    SMS = "sms"


class OtpPurpose(models.TextChoices):
    SIGNUP = "signup"
    PHONE = "phone"
    RESET = "reset"


class VerificationCode(models.Model):
    code_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="codes")
    channel = models.CharField(max_length=10, choices=OtpChannel.choices)
    purpose = models.CharField(max_length=10, choices=OtpPurpose.choices)
    code_hash = models.TextField()
    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=5)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "verification_code"
        indexes = [models.Index(fields=["account", "purpose", "-created_at"])]


class VerificationType(models.TextChoices):
    SHELTER_ORG = "shelter_org"
    RESCUER = "rescuer"
    PROVIDER = "provider"


class VerificationStatus(models.TextChoices):
    PENDING = "pending"
    NEEDS_INFO = "needs_info"
    APPROVED = "approved"
    REJECTED = "rejected"


class CapabilityStatus(models.TextChoices):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class VerificationRequest(models.Model):
    verification_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="verifications")
    type = models.CharField(max_length=20, choices=VerificationType.choices)
    status = models.CharField(max_length=20, choices=VerificationStatus.choices, default="pending")
    social_proof_url = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+", db_column="reviewed_by")
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    consent_at = models.DateTimeField(null=True, blank=True)
    consent_version = models.CharField(max_length=20, blank=True)

    class Meta:
        db_table = "verification_request"
        # The public-visibility predicate (listings/visibility.py) asks, per poster,
        # "does an approved shelter_org request exist?" — account_id + type + status.
        # The existing indexes don't serve it: idx_verification_account is the join key
        # alone, and idx_verification_one_open is PARTIAL on status IN (pending,
        # needs_info) — it deliberately excludes 'approved', so the hot read misses it.
        # Kept an index rather than denormalizing a verification_status column onto
        # shelter_profile: "verified is always derived, never a stored boolean" is a
        # load-bearing rule (Sprint 1 §conventions, Sprint 2 rule 1, Decision B).
        # See dev/verification-and-admin.md for the re-open trigger.
        indexes = [models.Index(fields=["account", "type", "status"],
                                name="idx_verification_acct_type_st")]


class VerificationDocument(models.Model):
    document_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    verification = models.ForeignKey(VerificationRequest, on_delete=models.CASCADE,
                                     related_name="documents")
    doc_type = models.CharField(max_length=30)
    file_url = models.TextField()
    uploaded_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default="pending")
    review_note = models.TextField(blank=True)
    # Per-document review (US-R6). These columns exist in the §7 DDL but were missing from
    # this model until Sprint 2 wired the reviewer — see kupkop_mvp_schema.sql. reviewed_by/
    # _at stamp who bounced a file and when; superseded_by links a rejected file to the
    # replacement inserted on resubmit (US-V3), keeping the rejected row for the audit trail.
    reviewed_by = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+", db_column="reviewed_by")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    superseded_by = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="+", db_column="superseded_by")
    # US-SEC4 · set the moment purge_expired_documents nulls file_url (90 days after the
    # owning request's terminal decision, RA 10173 data-minimization). The row survives —
    # only the image goes — so this timestamp is the audit trail's "and here's when it
    # stopped existing," distinct from uploaded_at/reviewed_at.
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "verification_document"


class CapabilityType(models.TextChoices):
    RESCUER = "rescuer"
    PROVIDER = "provider"


class AccountCapability(models.Model):
    capability_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="capabilities")
    capability = models.CharField(max_length=20, choices=CapabilityType.choices)
    status = models.CharField(max_length=20, choices=CapabilityStatus.choices, default="pending")
    granted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "account_capability"
        constraints = [models.UniqueConstraint(fields=["account", "capability"],
                                               name="uq_account_capability")]


class VerificationAccessLog(models.Model):
    """US-SEC3 · who saw a verification request's identity documents, and when.

    Decisions were already attributable (VerificationRequest.reviewed_by/_at) — this
    covers *views*, the RA 10173 accountability gap: a reviewer can look at a gov ID
    without ever deciding on it. One row per admin change-view load (see
    VerificationRequestAdmin.change_view); append-only, no admin registration, no
    update/delete path — a viewing log that could itself be edited proves nothing.
    """
    log_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    verification = models.ForeignKey(VerificationRequest, on_delete=models.CASCADE,
                                     related_name="access_log")
    # A staffer can view before accounts.staff.reviewer_account() resolves them to an
    # Account (e.g. is_staff=True with no StaffProfile yet) — staff_username is always
    # captured so a view is never silently unattributed; viewer is the derived Account
    # when the staff bridge (US-R1) links one.
    viewer = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="+", db_column="viewer_account_id")
    staff_username = models.CharField(max_length=150)
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "verification_access_log"
        indexes = [models.Index(fields=["verification", "-viewed_at"])]
