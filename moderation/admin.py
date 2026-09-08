"""US-M2 · the moderation queue — the platform-ops admin surface for `moderation_flag`,
same pattern as the verification queue (US-R2): oldest-open-first, decisions attributed
via the staff bridge (accounts.staff.reviewer_account), never hand-edited.
"""
from django.contrib import admin, messages
from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from accounts.staff import reviewer_account
from moderation.actions import resolve_flag
from moderation.models import FlagStatus, ModerationFlag


# ⚠️ UNREGISTERED by US-X2 (2026-09-06). Replaced by the console: /moderation
# (Sprint 10 US-M1–M3). The class is KEPT, not deleted — it is the cheapest possible rollback,
# and reverting the US-X2 commit restores this admin exactly as it was.
# Re-registering it would put a second writer on the same rows, with none of the
# console's audit-log or access-log coverage.
# @admin.register(ModerationFlag)
class ModerationFlagAdmin(admin.ModelAdmin):
    list_display = ("target_type", "target_id", "reporter", "reason", "status", "created_at")
    list_filter = ("status", "target_type")
    readonly_fields = ("reporter_account", "target_type", "target_id", "reason", "status",
                       "reviewed_by", "created_at", "reviewed_at")
    actions = ["mark_actioned", "mark_dismissed"]

    @admin.display(description="Reporter")
    def reporter(self, obj):
        # ⚠️ NULL = system-raised (e.g. the repeat-withdrawal rule), not a person — the
        # Sprint 2 out-of-scope note that got lost; honored here (US-M2).
        return obj.reporter_account.email if obj.reporter_account else "System"

    @admin.action(description="Mark actioned (something was done about it)")
    def mark_actioned(self, request, queryset):
        self._resolve(request, queryset, FlagStatus.ACTIONED)

    @admin.action(description="Dismiss (no action needed)")
    def mark_dismissed(self, request, queryset):
        self._resolve(request, queryset, FlagStatus.DISMISSED)

    def _resolve(self, request, queryset, status):
        reviewer = reviewer_account(request.user)
        if reviewer is None:
            self.message_user(
                request,
                "Your admin login isn't linked to a reviewer account — decision not "
                "recorded. Run `manage.py createstaff` to link it.", level=messages.ERROR)
            return
        # ⚠️ Delegates to moderation/actions.py rather than implementing the transition here.
        # US-T3's story-hiding used to live in this method only — and Sprint 10 US-X2 deletes
        # this admin. Behaviour that lives in the surface being removed goes with it, and
        # nobody notices until a story flag is actioned somewhere else and the story stays
        # visible. One writer, invoked by both surfaces.
        n = hidden = 0
        for flag in queryset:
            _, effects = resolve_flag(flag, reviewer, status)
            n += 1
            hidden += 1 if effects.get("story_hidden") else 0
        note = f"{n} flag(s) marked {status}."
        if hidden:
            note += f" {hidden} story(ies) hidden."
        self.message_user(request, note, level=messages.SUCCESS)

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("reporter_account")
        # A queue, not a table: open flags (actionable) sort to the top, oldest first —
        # same rank pattern as VerificationRequestAdmin's queue.
        return qs.annotate(
            _queue_rank=Case(When(status=FlagStatus.OPEN, then=Value(0)),
                             default=Value(1), output_field=IntegerField()),
        ).order_by("_queue_rank", "created_at")

    def has_add_permission(self, request):
        # Flags are created by the API (or, per the DDL, a system rule) — never typed in.
        return False

    def has_delete_permission(self, request, obj=None):
        # A flag is the audit trail of a moderation concern — never deletable here.
        return False
