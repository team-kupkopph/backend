import uuid

from django.db import models

from accounts.models import Account


class FlagTarget(models.TextChoices):
    ACCOUNT = "account"
    LISTING = "listing"
    REPORT = "report"
    QR = "qr"
    MESSAGE = "message"
    STORY = "story"   # D-S6-4 · stories are UGC and must be flaggable via the same pipeline
    SHIFT = "shift"   # P6 · G21 — volunteers can report a bad shift listing


class ReasonCode(models.TextChoices):
    """Review step 7g (MQ3) · what kind of problem a report is. Welfare concerns can be sorted ahead of spam, and a
    report can be broken down by type. `other` needs a note; the rest do not."""
    SPAM_SCAM = "spam_scam"
    ANIMAL_WELFARE = "animal_welfare"
    HARASSMENT = "harassment"
    MISLEADING = "misleading"
    OTHER = "other"


class FlagStatus(models.TextChoices):
    OPEN = "open"
    REVIEWED = "reviewed"
    ACTIONED = "actioned"
    DISMISSED = "dismissed"


# Statuses a flag can still be moved from. `open` and `reviewed` are working states; `actioned` and `dismissed` are
# terminal, and a second decision would overwrite the first reviewer's stamp with no record that it happened. Also what
# counts as a flag still "live" about a target (review step 5). Lives here so both moderation.actions and
# moderation.targets read one definition.
DECIDABLE = {FlagStatus.OPEN, FlagStatus.REVIEWED}


class ModerationFlag(models.Model):
    """A "report this" flag on some other row (matches kupkop_mvp_schema.sql
    `moderation_flag`). `target_type`/`target_id` point at the flagged row generically
    (no FK — the targets span several apps: listings, sagip, shelter) rather than one
    column per target type.

    `reporter_account` is nullable — a schema-level accommodation (§ DDL history) for
    system-raised flags (e.g. a repeat-withdrawal rule), not something this sprint's
    stories create; every flag Track M itself writes has a reporter.
    """

    flag_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reporter_account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name="moderation_flags_raised")
    target_type = models.CharField(max_length=10, choices=FlagTarget.choices)
    target_id = models.UUIDField()
    reason = models.TextField(blank=True)   # the reporter's note: required only for `other` (the API enforces it)
    reason_code = models.CharField(max_length=20, choices=ReasonCode.choices, default=ReasonCode.OTHER)
    status = models.CharField(max_length=10, choices=FlagStatus.choices, default=FlagStatus.OPEN)
    reviewed_by = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+", db_column="reviewed_by")
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "moderation_flag"
        indexes = [models.Index(fields=["status"], name="idx_flag_status"),
                  models.Index(fields=["target_type", "target_id"], name="idx_flag_target")]
        constraints = [
            # Review step 15 (MO12) · one LIVE report per person per target, held by the database: two taps in the same instant
            # cannot write two rows. Decided reports are history and do not count; a system flag has no reporter, and NULLs are
            # distinct, so those are unaffected. See moderation/reports.py.
            models.UniqueConstraint(fields=["reporter_account", "target_type", "target_id"],
                                    condition=models.Q(status__in=["open", "reviewed"]),
                                    name="uq_flag_one_live_per_reporter_target"),
        ]


class AccountBlock(models.Model):
    """Review step 7d · `blocker` does not want to see, or be seen by, `blocked`. Both stores require a way to block
    abusive users. A block is one row and works in BOTH directions (see `moderation.blocks.hidden_from`); it never
    tells the blocked person, and it never touches something already under way between the two.

    Deleted with either account: a block about a departed person is not a record worth keeping."""

    block_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    blocker = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="blocks_made")
    blocked = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="blocks_received")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "account_block"
        constraints = [
            models.UniqueConstraint(fields=["blocker", "blocked"], name="uq_account_block_pair"),
            models.CheckConstraint(condition=~models.Q(blocker=models.F("blocked")), name="account_block_not_self"),
        ]
        indexes = [models.Index(fields=["blocked"], name="idx_account_block_blocked")]
