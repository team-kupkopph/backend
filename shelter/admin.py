"""US-X3 · the reviewer-checked donation-QR flag, in the platform-ops admin.

Donations are a two-key gate (see `ShelterDashboardView`): the org must be approved AND its
donation QR verified here. The applicant-submitted fields are read-only; a reviewer flips
`verified` only after eyeballing the QR image, via the actions below. The upload path that
creates these rows is the Sprint-4 donations UI — hand-add is disabled.
"""
from django.contrib import admin, messages

from shelter.models import DonationQr


# ⚠️ UNREGISTERED by US-X2 (2026-09-06). Replaced by the console: /donations
# (Sprint 10 US-Q1–Q2). The class is KEPT, not deleted — it is the cheapest possible rollback,
# and reverting the US-X2 commit restores this admin exactly as it was.
# Re-registering it would put a second writer on the same rows, with none of the
# console's audit-log or access-log coverage.
# @admin.register(DonationQr)
class DonationQrAdmin(admin.ModelAdmin):
    list_display = ("account", "provider", "account_name", "verified", "created_at")
    list_filter = ("verified", "provider")
    search_fields = ("account_name",)
    readonly_fields = ("account", "provider", "account_name", "qr_image_url", "verified", "created_at")
    actions = ["mark_verified", "unmark_verified"]

    @admin.action(description="Verify QR (opens donations once the org is approved)")
    def mark_verified(self, request, queryset):
        n = queryset.update(verified=True)
        self.message_user(request, f"{n} donation QR(s) verified.", level=messages.SUCCESS)

    @admin.action(description="Un-verify QR (closes the donations gate)")
    def unmark_verified(self, request, queryset):
        n = queryset.update(verified=False)
        self.message_user(request, f"{n} donation QR(s) un-verified.", level=messages.WARNING)

    def has_add_permission(self, request):
        return False  # QRs are created by the Sprint-4 donations UI, not hand-entered here
