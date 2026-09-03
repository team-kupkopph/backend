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


class FlagStatus(models.TextChoices):
    OPEN = "open"
    REVIEWED = "reviewed"
    ACTIONED = "actioned"
    DISMISSED = "dismissed"


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
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=FlagStatus.choices, default=FlagStatus.OPEN)
    reviewed_by = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "moderation_flag"
        indexes = [models.Index(fields=["status"], name="idx_flag_status"),
                  models.Index(fields=["target_type", "target_id"], name="idx_flag_target")]
