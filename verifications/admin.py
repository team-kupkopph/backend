"""The reviewer surface — the verification queue (US-R2).

This is the platform-ops Django admin, not the mobile shelter dashboard and not the
Phase-2 React console. The list is a work queue: everything waiting on a reviewer, oldest
first. Per-document review, the tier-derived checklist and the approve/reject actions
(US-R3–R6) build on this registration.
"""
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.template.response import TemplateResponse
from django.utils.html import format_html_join

from accounts.staff import reviewer_account
from common.storage import signed_get_url
from shelter.models import ShelterProfile
from verifications.models import VerificationAccessLog, VerificationRequest
from verifications.review import (
    ReviewError,
    approve_request,
    reject_request,
    request_more_info,
    review_document,
)
from verifications.rules import review_checklist


@admin.register(VerificationRequest)
class VerificationRequestAdmin(admin.ModelAdmin):
    list_display = ("applicant", "type", "tier", "submitted_at", "status")
    list_filter = ("status", "type")
    actions = ["approve_selected", "reject_selected", "needs_info"]
    # The detail page is a read-only review surface: a decision must be attributable and
    # gate-correct, so every mutation goes through the approve/reject/needs_info actions,
    # never a hand-edit of the change form. All fields are therefore read-only.
    readonly_fields = ("account", "type", "status", "social_proof_url", "submitted_at",
                       "reviewed_by", "reviewed_at", "notes", "consent_at", "consent_version",
                       "required_docs_check", "documents_preview")

    @admin.display(description="Documents")
    def documents_preview(self, obj):
        """All of an applicant's files on one screen (US-R3), via short-lived signed URLs
        (never a raw public link). rescue_photos render as one labelled group, not four
        identical rows. Each entry carries its doc_type and per-file status."""
        docs = list(obj.documents.all())
        if not docs:
            return "— (no documents)"
        photos = [d for d in docs if d.doc_type == "rescue_photos"]
        singles = [d for d in docs if d.doc_type != "rescue_photos"]
        groups = [(d.doc_type, d.status, [d]) for d in singles]
        if photos:
            statuses = {p.status for p in photos}
            label = f"rescue_photos ({len(photos)})"
            groups.append((label, "mixed" if len(statuses) > 1 else photos[0].status, photos))
        return format_html_join(
            "",
            '<div style="margin:8px 0"><strong>{}</strong> &middot; {}<br>{}</div>',
            ((label, status, self._thumbs(ds)) for label, status, ds in groups))

    def _thumbs(self, docs):
        return format_html_join(
            "",
            '<img src="{}" style="height:88px;margin:3px;border:1px solid #ccc;'
            'border-radius:4px" loading="lazy">',
            ((signed_get_url(d.file_url),) for d in docs))

    @admin.display(description="Required documents (tier-derived)")
    def required_docs_check(self, obj):
        """The reviewer's safety readout (US-R4): the required set computed from the
        applicant's stored tier vs what's on file — so an NGO can't be approved on
        community-rescue evidence. Informs only; the decision stays human."""
        if obj.type != "shelter_org":
            return "— (member verification has no tier document set)"
        sp = ShelterProfile.objects.filter(account=obj.account).first()
        if sp is None:
            return "— (no shelter profile)"
        present = [d.doc_type for d in obj.documents.all()]
        missing, deferred = review_checklist(sp.tier, present)
        parts = []
        if missing:
            parts.append("Missing: " + ", ".join(missing))
        if deferred:
            parts.append("Deferred (BAI pending): " + ", ".join(deferred))
        return " · ".join(parts) if parts else "All required documents present."

    @admin.action(description="Approve selected")
    def approve_selected(self, request, queryset):
        reviewer = self._reviewer_or_refuse(request)
        if reviewer is None:
            return
        for vr in queryset:
            approve_request(vr, reviewer)
        self.message_user(request, f"Approved {queryset.count()} request(s).",
                          level=messages.SUCCESS)

    @admin.action(description="Reject selected (with a reason)")
    def reject_selected(self, request, queryset):
        reviewer = self._reviewer_or_refuse(request)
        if reviewer is None:
            return
        if "apply" in request.POST:
            try:
                for vr in queryset:
                    reject_request(vr, reviewer, request.POST.get("notes", ""))
            except ReviewError as exc:
                # nothing was committed — reject_request raises before saving, per-call atomic
                self.message_user(request, str(exc), level=messages.ERROR)
                return
            self.message_user(request, f"Rejected {queryset.count()} request(s).",
                              level=messages.SUCCESS)
            return
        # first pass: collect the reason. The note is shown to the applicant, so it can't
        # be supplied by a bulk action alone — render an intermediate page for it.
        return TemplateResponse(request, "admin/verifications/reject_action.html", {
            **self.admin_site.each_context(request),
            "title": "Reject with a reason",
            "queryset": queryset,
            "opts": self.model._meta,
        })

    @admin.action(description="Ask for more info (reject files, bounce back)")
    def needs_info(self, request, queryset):
        reviewer = self._reviewer_or_refuse(request)
        if reviewer is None:
            return
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one request to review its documents.",
                              level=messages.ERROR)
            return
        vr = queryset.first()
        if "apply" in request.POST:
            try:
                # Atomic: if any per-file reason (or the overall note) is missing, the whole
                # bounce is refused rather than half-applied — the applicant never gets a
                # partial, confusing state.
                with transaction.atomic():
                    for doc in vr.documents.all():
                        if request.POST.get(f"reject_{doc.pk}"):
                            review_document(doc, reviewer, "rejected",
                                            request.POST.get(f"note_{doc.pk}", ""))
                    request_more_info(vr, reviewer, request.POST.get("notes", ""))
            except ReviewError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
                return
            self.message_user(request, "Sent back to the applicant for more info.",
                              level=messages.SUCCESS)
            return
        return TemplateResponse(request, "admin/verifications/needs_info_action.html", {
            **self.admin_site.each_context(request),
            "title": "Ask for more info",
            "verification": vr,
            "documents": vr.documents.all(),
            "opts": self.model._meta,
        })

    def _reviewer_or_refuse(self, request):
        """The admin Account the acting staff user is attributed to, or None (with an
        error surfaced) — a decision is never recorded anonymously (US-R5)."""
        reviewer = reviewer_account(request.user)
        if reviewer is None:
            self.message_user(
                request,
                "Your admin login isn't linked to a reviewer account — decision not "
                "recorded. Run `manage.py createstaff` to link it.",
                level=messages.ERROR)
        return reviewer

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("account")
        # A queue, not a table: pending (the reviewer's actionable set) sorts to the top,
        # oldest-submitted first within it, so nothing waits unseen. needs_info/rejected/
        # approved are decided or waiting on the applicant, so they fall below.
        return qs.annotate(
            _queue_rank=Case(When(status="pending", then=Value(0)),
                             default=Value(1), output_field=IntegerField()),
        ).order_by("_queue_rank", "submitted_at")

    def has_add_permission(self, request):
        # Requests are created by applicants via the API, never typed into the admin.
        return False

    def has_delete_permission(self, request, obj=None):
        # A verification request is the audit trail of a trust decision — never deletable
        # from the reviewer surface.
        return False

    def change_view(self, request, object_id, form_url="", extra_context=None):
        # US-SEC3 · this is the one page that renders identity documents
        # (documents_preview, above) — log the view itself, not just eventual decisions.
        # Logged unconditionally (GET render or a POST that re-renders, e.g. needs_info's
        # intermediate page still shows documents_preview) rather than gated on method,
        # since the documents are on the page either way. staff_username is captured even
        # when reviewer_account() can't resolve one, so a view is never unattributed.
        # Guarded on existence first — a bad/deleted object_id must still 404 cleanly
        # rather than raise on an FK to nothing.
        if self.get_queryset(request).filter(pk=unquote(object_id)).exists():
            VerificationAccessLog.objects.create(
                verification_id=object_id, viewer=reviewer_account(request.user),
                staff_username=request.user.get_username())
        return super().change_view(request, object_id, form_url, extra_context)

    @admin.display(description="Applicant")
    def applicant(self, obj):
        return obj.account.email

    @admin.display(description="Tier")
    def tier(self, obj):
        # The required-doc set is derived from shelter_profile.tier (§3.5), so the tier is
        # the reviewer's first orienting fact. Members (type='rescuer') have no shelter
        # profile — shown as an em dash, not a blank.
        sp = ShelterProfile.objects.filter(account=obj.account).first()
        return sp.get_tier_display() if sp else "—"
