"""US-D1 · the admin audit log.

⚠️ APPEND-ONLY. There is no update or delete path in this codebase, and the model is
registered in no admin. `scripts`-side that is a convention; here it is enforced by there
being nothing to call. An audit log that can itself be edited proves nothing.
"""
import uuid

from django.db import models

from accounts.models import Account


class AdminAuditLog(models.Model):
    audit_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ⚠️ NULLABLE actor, NOT NULL label — the same shape verification_access_log uses.
    # A failed login has no authenticated actor and is precisely the event a security audit
    # most needs; requiring actor_id would force the middleware to skip exactly those rows.
    actor = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+", db_column="actor_id")
    # Always populated: the staff username once known, otherwise the submitted identifier or
    # "anonymous". An action is never silently unattributed.
    actor_label = models.CharField(max_length=150)

    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=40)
    target_id = models.UUIDField(null=True, blank=True)
    outcome = models.CharField(max_length=20)
    detail = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "admin_audit_log"
        indexes = [
            models.Index(fields=["actor", "-created_at"], name="idx_audit_actor"),
            models.Index(fields=["target_type", "target_id", "-created_at"], name="idx_audit_target"),
        ]

    def __str__(self):
        return f"{self.actor_label} {self.action} -> {self.outcome}"
