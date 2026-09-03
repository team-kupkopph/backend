import uuid

from django.contrib.gis.db import models  # GeoDjango: PointField + spatial manager

from accounts.models import Account


class ReportType(models.TextChoices):
    STRAY = "stray"
    LOST = "lost"
    FOUND = "found"


class Species(models.TextChoices):
    DOG = "dog"
    CAT = "cat"
    OTHER = "other"


class StrayCondition(models.TextChoices):
    INJURED = "injured"
    SICK = "sick"
    HEALTHY = "healthy"
    PREGNANT = "pregnant"


class StrayStatus(models.TextChoices):
    # Only 'reported' is amber (someone still needs to act). Sprint 3 owns the transitions.
    REPORTED = "reported"
    CLAIMED = "claimed"
    RESCUED = "rescued"
    SAFE = "safe"
    RESOLVED = "resolved"


# Defined here (not imported from listings) because listings.models imports sagip.models —
# importing back would cycle. The DB enum values are the same shared `sex`/`size_category` types.
class Sex(models.TextChoices):
    MALE = "male"
    FEMALE = "female"
    UNKNOWN = "unknown"


class SizeCategory(models.TextChoices):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class MatchStatus(models.TextChoices):
    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


class StrayReport(models.Model):
    """A stray sighting (matches kupkop_mvp_schema.sql `stray_report`). `geom` is the one
    precise-GPS surface in the app (decision 11) — a PostGIS point; everywhere else a
    person's location is a city. `reporter_account_id` is always stored even when
    `is_anonymous` (which only hides the reporter from *other users*, not from the row)."""

    report_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report_type = models.CharField(max_length=10, choices=ReportType.choices,
                                   default=ReportType.STRAY)
    reporter_account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name="stray_reports")
    # pet_id exists in the DDL (lost & found links a report to a pet), but there is no Pet
    # Django model yet — kept as a nullable id column so the schema stays faithful.
    pet_id = models.UUIDField(null=True, blank=True)
    is_anonymous = models.BooleanField(default=False)
    species = models.CharField(max_length=10, choices=Species.choices)
    condition = models.CharField(max_length=10, choices=StrayCondition.choices)
    notes = models.TextField(blank=True)
    # D-S6-1 · describable fields the §11 matcher scores lost<->found on. Nullable and
    # meaningful only for report_type lost/found; a stray sighting leaves them blank. Stored on
    # the report (a lost report may prefill them from the linked pet), never re-read live, so a
    # later pet edit can't rewrite what the reporter actually described.
    breed = models.CharField(max_length=80, null=True, blank=True)
    color_markings = models.CharField(max_length=120, null=True, blank=True)
    size_category = models.CharField(max_length=10, choices=SizeCategory.choices,
                                     null=True, blank=True)
    sex = models.CharField(max_length=10, choices=Sex.choices, null=True, blank=True)
    geom = models.PointField(geography=True, srid=4326)   # NOT NULL — a report needs a place
    location_text = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=10, choices=StrayStatus.choices,
                              default=StrayStatus.REPORTED)
    escalation_level = models.SmallIntegerField(default=0)  # 0 as-reported · 1 ~5km · 2 partners
    # Derived from geom (reverse-geocode) at write; NULL if unresolved — out of MVP scope.
    city = models.CharField(max_length=80, null=True, blank=True)
    barangay = models.CharField(max_length=80, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stray_report"


class StrayReportPhoto(models.Model):
    photo_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey(StrayReport, on_delete=models.CASCADE, related_name="photos")
    url = models.TextField()
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stray_report_photo"


class RescueCase(models.Model):
    """US-S6 scaffolding · an exclusive, binding claim on a stray report (kupkop_mvp_schema.sql
    `rescue_case`). Sprint 3 owns the claim flow that writes these; this sprint only stands the
    table up. `report` is UNIQUE among ACTIVE claims (partial unique, expired_at IS NULL), not
    unique forever: a stalled claim can expire (Sprint 3) and the report reverts to `reported` to
    be re-claimed, while the expired row survives as the accountability signal — so exclusivity is
    'one live claim', matching the DDL's `idx_rescue_case_active`, not the pre-expiry plain UNIQUE."""

    case_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey(StrayReport, on_delete=models.CASCADE, related_name="cases")
    claimed_by_account = models.ForeignKey(Account, on_delete=models.PROTECT,
                                           related_name="claimed_cases")
    claimed_at = models.DateTimeField(auto_now_add=True)
    outcome_notes = models.TextField(blank=True)
    outcome_photo_url = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    # Set when a stalled claim auto-expires (Sprint 3); the report reverts to reported and can be
    # re-claimed while this row is kept. NULL = active claim.
    expired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rescue_case"
        constraints = [
            models.UniqueConstraint(fields=["report"],
                                    condition=models.Q(expired_at__isnull=True),
                                    name="idx_rescue_case_active"),
        ]


class OfferType(models.TextChoices):
    TRANSPORT = "transport"
    VET_COSTS = "vet_costs"
    SUPPLIES = "supplies"


class OfferStatus(models.TextChoices):
    OPEN = "open"
    MATCHED = "matched"
    EXPIRED = "expired"


class ReportOffer(models.Model):
    """A non-exclusive commitment on an unclaimed report (decision 12; matches
    kupkop_mvp_schema.sql `report_offer`). Sprint 3's Track O (US-O1) owns creating these;
    this model lands here first because Track K (build first) needs it to exist — claiming
    a report flips every OPEN offer on it to MATCHED (US-K1). `UNIQUE(report, account,
    offer_type)`: one person may offer transport AND supplies, never the same type twice."""

    offer_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey(StrayReport, on_delete=models.CASCADE, related_name="offers")
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="report_offers")
    offer_type = models.CharField(max_length=10, choices=OfferType.choices)
    status = models.CharField(max_length=10, choices=OfferStatus.choices,
                              default=OfferStatus.OPEN)
    note = models.CharField(max_length=200, blank=True)
    # created_at + 48h (decision 14: must exceed the longest claim window, 24h) — set by the
    # US-O1 create endpoint, not defaulted here (the model has no opinion on the policy number).
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "report_offer"
        constraints = [
            models.UniqueConstraint(fields=["report", "account", "offer_type"],
                                    name="uq_report_offer_type"),
        ]
        indexes = [
            models.Index(fields=["report", "status"], name="idx_report_offer_report"),
            models.Index(fields=["account", "-created_at"], name="idx_report_offer_account"),
        ]


class CaseStatusHistory(models.Model):
    """US-S6 · an append-only audit of every stray-report status change (kupkop_mvp_schema.sql
    `case_status_history`). Written only via `set_report_status()` so a status can never move
    unlogged — Sprint 3 builds the claim/resolve transitions on top of that single writer."""

    history_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey(StrayReport, on_delete=models.CASCADE, related_name="status_history")
    status = models.CharField(max_length=10, choices=StrayStatus.choices)
    # SET_NULL, not CASCADE: an account leaving must not erase the audit trail of what it changed.
    changed_by_account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True,
                                           blank=True, related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "case_status_history"
        indexes = [models.Index(fields=["report", "-changed_at"], name="idx_case_history_report")]


class ReportMatch(models.Model):
    """US-L2 · a suggested lost<->found match (kupkop_mvp_schema.sql `report_match`).

    The §11 matcher writes these with status `suggested` and a [0,1] score; a human always
    confirms or dismisses (matching never auto-resolves). `report` is the newer report being
    matched, `matched_report` the candidate. UNIQUE(report, matched_report) + a
    report != matched_report check keep a pair from duplicating or self-matching (re-runs
    refresh the score in place). A decision is applied to BOTH directions of a pair by the view.
    """

    match_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey(StrayReport, on_delete=models.CASCADE, related_name="matches")
    matched_report = models.ForeignKey(StrayReport, on_delete=models.CASCADE, related_name="+")
    score = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    status = models.CharField(max_length=10, choices=MatchStatus.choices,
                              default=MatchStatus.SUGGESTED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "report_match"
        constraints = [
            models.UniqueConstraint(fields=["report", "matched_report"],
                                    name="idx_report_match_pair"),
            models.CheckConstraint(condition=~models.Q(report=models.F("matched_report")),
                                   name="report_match_not_self"),
        ]
        indexes = [
            models.Index(fields=["matched_report"], name="idx_report_match_matched"),
            models.Index(fields=["status"], name="idx_report_match_status"),
        ]
