import uuid

from django.contrib.auth.hashers import check_password, make_password
from django.db import models, transaction


class CICharField(models.CharField):
    """Case-insensitive char backed by the Postgres `citext` type (matches
    kupkop_mvp_schema.sql). Django 5.1 removed contrib.postgres CICharField;
    this maps the column to citext so uniqueness/compares are case-insensitive
    while the stored case is preserved. Requires the citext extension migration."""

    def db_type(self, connection):
        return "citext"


class AccountType(models.TextChoices):
    PERSONAL = "personal"
    SHELTER = "shelter"
    ADMIN = "admin"


class AccountStatus(models.TextChoices):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class AccountManager(models.Manager):
    def create_account(self, *, account_type, email, display_name, password=None,
                       terms_consent_version=None, **extra):
        # RA 10173: the controller must be able to DEMONSTRATE consent, so creating an
        # account always stamps it — signing up *is* the consent the screen describes.
        # Stamped here rather than in the view so every creation path (email signup,
        # social signup) records it and none can quietly forget.
        from django.conf import settings as dj_settings
        from django.utils import timezone
        with transaction.atomic():
            account = self.model(account_type=account_type, email=email,
                                 display_name=display_name, **extra)
            if password:
                account.set_password(password)
            account.terms_consent_at = timezone.now()
            account.terms_consent_version = (
                terms_consent_version or getattr(dj_settings, "TERMS_VERSION", ""))
            account.save(using=self._db)
            AccountSettings.objects.create(account=account)
        return account


class Account(models.Model):
    account_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account_type = models.CharField(max_length=20, choices=AccountType.choices)
    email = CICharField(max_length=254, unique=True)
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    password_hash = models.TextField(blank=True)          # NULL/"" for social-only
    display_name = models.CharField(max_length=100)
    photo_url = models.TextField(blank=True)
    # Signup Terms/Privacy consent (RA 10173). Distinct from
    # verification_request.consent_at/.consent_version, which cover the separate
    # purpose-specific consent to collect identity DOCUMENTS (§12.6) — never merge them.
    # Nullable: accounts predating this column have no value and must not be backfilled.
    terms_consent_at = models.DateTimeField(null=True, blank=True)
    terms_consent_version = models.CharField(max_length=20, blank=True)
    # date_of_birth intentionally omitted (RA 10173 minimization — see plan Global Constraints)
    two_factor_enabled = models.BooleanField(default=False)
    sessions_revoked_at = models.DateTimeField(null=True, blank=True)  # logout-all / password reset set this; tokens with iat before it are rejected
    status = models.CharField(max_length=20, choices=AccountStatus.choices,
                              default=AccountStatus.ACTIVE)
    # US-C1 · both columns have been in the documented DDL since the data-analyst review
    # and in NO model or migration until now — table-level drift checking could not see
    # it (the D-S5-1 blind spot; check-docs now compares columns too).
    # last_active_at: retention/inactive-account policy input (§12.6). Never a login side
    # effect that writes on every request — whoever starts using it decides the cadence.
    last_active_at = models.DateTimeField(null=True, blank=True)
    # deleted_at: opens the §12.7 soft-delete grace window. Moves in lockstep with
    # status='deleted' — enforced below by the M5 CHECK, not by convention.
    deleted_at = models.DateTimeField(null=True, blank=True)
    # anonymized_at: CLOSES that window (D-S7-1). NULL = never purged, i.e. either active or
    # soft-deleted and still inside the grace period. Set once, by the purge sweep, which is
    # what makes the sweep idempotent — it skips rows already stamped.
    anonymized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AccountManager()

    USERNAME_FIELD = "email"
    is_active = True            # required by SimpleJWT/auth contracts

    class Meta:
        db_table = "account"
        constraints = [
            # M5 (root DDL): a soft delete is two facts that must never disagree — a
            # status of 'deleted' with no deleted_at has no grace window to expire, and a
            # deleted_at on an active account would make the purge sweep eat a live user.
            # Postgres enforces the pair; application code cannot set one without the other.
            models.CheckConstraint(
                condition=models.Q(deleted_at__isnull=False, status=AccountStatus.DELETED)
                | models.Q(deleted_at__isnull=True) & ~models.Q(status=AccountStatus.DELETED),
                name="chk_account_deleted_consistency",
            ),
        ]

    def set_password(self, raw):
        self.password_hash = make_password(raw)

    def check_password(self, raw):
        return bool(self.password_hash) and check_password(raw, self.password_hash)

    @property
    def is_authenticated(self):
        return True


class StaffProfile(models.Model):
    """Option A staff bridge (US-R1). A Kupkop reviewer signs into `/admin` as a Django
    `contrib.auth.User` (web session), but a review decision is attributed to an
    `Account(account_type='admin')` via `verification_request.reviewed_by`. This 1:1 link
    is the join between the two identities, so `reviewed_by` can be stamped from the admin
    request's `User`.

    The `auth.User` half lives only in Django, like `auth_user` and the JWT blacklist tables.
    `staff_profile` itself does NOT: it is a documented domain table, `kupkop_mvp_schema.sql`
    line 137. It has to be. `dev/check-docs.py::check_schema_vs_migrations` scans every
    project migration for a `db_table` and errors on any that has no `CREATE TABLE` in the
    schema SQL — code ahead of docs is an error by design — so `0006_staffprofile.py` would
    fail the docs CI if this table were undocumented.

    (Corrected 2026-09-06, Sprint 9 US-N1. This docstring previously claimed the opposite,
    which is the comment someone would trust when deciding where to put the next staff
    table — and following it would have turned the docs build red.)"""

    id = models.BigAutoField(primary_key=True)
    user = models.OneToOneField("auth.User", on_delete=models.CASCADE,
                                related_name="staff_profile")
    account = models.OneToOneField(Account, on_delete=models.CASCADE,
                                   related_name="staff_profile")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "staff_profile"


class AccountSettings(models.Model):
    settings_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name="settings")
    marketing_emails = models.BooleanField(default=False)
    approximate_location = models.BooleanField(default=True)
    masked_contact = models.BooleanField(default=True)
    push_enabled = models.BooleanField(default=True)
    # D-S7-3 · opt-in consent for CLIENT-SIDE / behavioural analytics only. The
    # server-authoritative aggregate events (common/analytics.py::emit, US-Y1) carry no
    # account identifiers and keep flowing regardless — that is why §17's tooling note says
    # they need no consent. Default false: silence is not consent under RA 10173.
    analytics_consent = models.BooleanField(default=False)
    # §12.6 puts the burden on the controller to DEMONSTRATE consent, which a bare boolean
    # cannot do — it cannot say WHEN. Cleared on withdrawal so the column always describes
    # the consent that is currently in force, never a lapsed one.
    analytics_consent_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "account_settings"


class AuthProvider(models.TextChoices):
    GOOGLE = "google"
    FACEBOOK = "facebook"
    APPLE = "apple"


class AccountIdentity(models.Model):
    identity_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="identities")
    provider = models.CharField(max_length=10, choices=AuthProvider.choices)
    provider_user_id = models.CharField(max_length=255)
    email = CICharField(max_length=254, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "account_identity"
        constraints = [
            models.UniqueConstraint(fields=["provider", "provider_user_id"],
                                    name="uq_provider_sub"),
            models.UniqueConstraint(fields=["account", "provider"], name="uq_account_provider"),
        ]


class Address(models.Model):
    address_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="addresses")
    label = models.CharField(max_length=40, blank=True)
    line1 = models.TextField(blank=True)
    barangay = models.CharField(max_length=80, blank=True)
    city = models.CharField(max_length=80)
    province = models.CharField(max_length=80, blank=True)
    postal_code = models.CharField(max_length=10, blank=True)
    geom = models.TextField(null=True, blank=True)   # PostGIS geography in prod; NULL for persons
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "address"
