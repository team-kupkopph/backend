"""The moderation queue, detail and decisions (US-M1/M2).

⚠️ `ModerationFlag` is one of the three registered model admins, so this track is what US-X1
demands before US-X2 can switch the Django admin off. It is not de-scopable.
"""
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery
from rest_framework.response import Response

from adminapi.pagination import decode_cursor, paginate
from adminapi.permissions import admin_account_for
from adminapi.verifications_views import StaffView
from community.models import StoryPost, StoryStatus
from community.moderation import StoryNotHidden, restore_story
from listings.models import AdoptionListing
from listings.moderation import ListingNotHidden, restore_listing
from moderation import telemetry
from moderation.actions import (
    AlreadyDecided,
    DecisionInvalid,
    ModerationError,
    TargetUnavailable,
    resolve_flag,
)
from moderation.models import DECIDABLE, FlagStatus, FlagTarget, ModerationFlag, ReasonCode
from moderation.offenders import RULES
from moderation.targets import describe_target, edited_since
from sagip.models import StrayReport
from sagip.moderation import ReportNotHidden, restore_report

SNIPPET_MAX = 120


def _snippet(flag):
    title = ((flag.target_snapshot or {}).get("title") or "").strip()
    return title if len(title) <= SNIPPET_MAX else title[:SNIPPET_MAX - 1].rstrip() + "…"


def flag_row(flag, *, with_target=False):
    return {
        "flag_id": str(flag.flag_id),
        "target_type": flag.target_type,
        "target_id": str(flag.target_id),
        "reason": flag.reason,
        "reason_code": flag.reason_code,   # review step 7g
        "status": flag.status,
        "created_at": flag.created_at.isoformat(),
        "reviewed_at": flag.reviewed_at.isoformat() if flag.reviewed_at else None,
        # Review step 19 (MO22) · who decided and when (the console used to say "Already actioned." with neither), and who
        # looked first. Names only: a reviewer's email is not the reporter's business or the page's.
        "reviewed_by": ({"account_id": str(flag.reviewed_by_id), "display_name": flag.reviewed_by.display_name}
                        if flag.reviewed_by_id else None),
        # Review step 8 · why it was taken down (the category the poster was told) and the moderator's internal note.
        # Review step 10 (MO26) · what was flagged, from the snapshot taken when it was reported (no query per row), and how many live
        # reports are about the same thing. `report_count` is present only where the queue annotated it.
        "snippet": _snippet(flag),
        # Review step 13 · a flag the platform raised says which rule raised it, so the console can say why a person is in the queue.
        "rule": flag.rule or None,
        "rule_label": RULES.get(flag.rule),
        **({"report_count": flag._live_count or 0} if hasattr(flag, "_live_count") else {}),
        "decision_reason_code": flag.decision_reason_code,
        "decision_note": flag.decision_note,
        "first_reviewed_at": flag.first_reviewed_at.isoformat() if flag.first_reviewed_at else None,
        "first_reviewed_by": ({"account_id": str(flag.first_reviewed_by_id), "display_name": flag.first_reviewed_by.display_name}
                              if flag.first_reviewed_by_id else None),
        # ⚠️ NULL reporter = system-raised (e.g. the repeat-withdrawal rule), not a person.
        # The column is nullable by schema accommodation; the UI must render "System" rather
        # than crash on a null.
        "reporter": (
            {"account_id": str(flag.reporter_account.account_id),
             "display_name": flag.reporter_account.display_name,
             "email": flag.reporter_account.email}
            if flag.reporter_account_id else None
        ),
        **(_with_target(flag) if with_target else {}),
    }


def _with_target(flag):
    """The target as it is now, what it was when reported, and whether those differ (review step 17, MO14)."""
    target = describe_target(flag.target_type, flag.target_id, flag_id=flag.flag_id)
    return {"target": target, "snapshot": flag.target_snapshot,
            "edited_since_reported": edited_since(flag.target_snapshot, target)}


class FlagQueueView(StaffView):
    """GET /admin-api/flags?status=&target_type=&reason_code=&q=&cursor=

    Review step 10 (MO26) · a queue a moderator can work: filter by kind and category, search the reporter's words, the reported
    thing's title and the reporter's name, and page on with `next_cursor`. Each row says what was flagged (`snippet`) and how many live
    reports are about it (`report_count`), both without a query per row."""

    def get(self, request):
        status = request.query_params.get("status", FlagStatus.OPEN)
        target_type = request.query_params.get("target_type")
        reason_code = request.query_params.get("reason_code")
        term = (request.query_params.get("q") or "").strip()

        qs = ModerationFlag.objects.select_related("reporter_account", "reviewed_by", "first_reviewed_by")
        if status != "all":
            if status not in FlagStatus.values:
                return Response({"error": {"code": "invalid_status"}}, status=400)
            qs = qs.filter(status=status)
        if target_type:
            if target_type not in FlagTarget.values:
                return Response({"error": {"code": "invalid_target_type"}}, status=400)
            qs = qs.filter(target_type=target_type)
        if reason_code:
            if reason_code not in ReasonCode.values:
                return Response({"error": {"code": "invalid_reason_code"}}, status=400)
            qs = qs.filter(reason_code=reason_code)
        if term:
            qs = qs.filter(Q(reason__icontains=term) | Q(target_snapshot__title__icontains=term)
                           | Q(reporter_account__display_name__icontains=term))
        live = (ModerationFlag.objects.filter(target_type=OuterRef("target_type"), target_id=OuterRef("target_id"),
                                              status__in=DECIDABLE)
                .order_by().values("target_type", "target_id").annotate(n=Count("pk")).values("n"))
        qs = qs.annotate(_live_count=Subquery(live, output_field=IntegerField()))

        # Oldest first, for the same reason the verification queue is: a moderation backlog is
        # a backlog. Uses the existing idx_flag_status / idx_flag_target.
        qs = qs.order_by("created_at", "flag_id")
        rows, next_cursor = paginate(qs, decode_cursor(request.query_params.get("cursor", "")),
                                     ["created_at", "flag_id"])
        return Response({"results": [flag_row(f) for f in rows], "next_cursor": next_cursor})


class FlagDetailView(StaffView):
    def get(self, request, flag_id):
        flag = (ModerationFlag.objects.select_related("reporter_account", "reviewed_by", "first_reviewed_by")
                .filter(flag_id=flag_id).first())
        if flag is None:
            return Response({"error": {"code": "not_found"}}, status=404)
        return Response(flag_row(flag, with_target=True))


class FlagDecisionView(StaffView):
    action = None

    def post(self, request, flag_id):
        target_status = {
            "review": FlagStatus.REVIEWED,
            "action": FlagStatus.ACTIONED,
            "dismiss": FlagStatus.DISMISSED,
        }[self.action]

        flag = ModerationFlag.objects.filter(flag_id=flag_id).first()
        if flag is None:
            return Response({"error": {"code": "not_found"}}, status=404)
        body = request.data if isinstance(request.data, dict) else {}
        reason_code, note = body.get("reason_code") or "", body.get("note") or ""
        if not isinstance(reason_code, str) or not isinstance(note, str):
            return Response({"error": {"code": "invalid_decision", "message": "reason_code and note must be text."}}, status=422)
        # The lock and the "already decided" guard live in resolve_flag (review step 18); this only translates its errors.
        try:
            flag, effects = resolve_flag(flag, admin_account_for(request), target_status, reason_code=reason_code, note=note)
        except AlreadyDecided as exc:
            return Response({"error": {"code": "already_decided", "status": exc.status}}, status=409)
        except DecisionInvalid as exc:
            return Response({"error": {"code": exc.code, "message": str(exc)}}, status=422)
        except TargetUnavailable as exc:
            # Nothing to action: the target is gone, or has no surface. Dismiss and review are still allowed.
            return Response({"error": {"code": f"target_{exc.state}", "message": str(exc)}}, status=422)
        except ModerationError as exc:
            return Response({"error": {"code": "invalid_decision", "message": str(exc)}}, status=422)

        flag.refresh_from_db()
        # Review step 14 (MO21) · the audit row names what was decided about, not only the flag: target, outcome, category, what changed.
        request._request._audit_extra = {"flagged_type": flag.target_type, "flagged_id": str(flag.target_id), "decision": target_status,
                                          "reason_code": flag.decision_reason_code or None, "effects": sorted(effects)}
        return Response({**flag_row(flag, with_target=True), "side_effects": effects})


class FlagReviewView(FlagDecisionView):
    action = "review"


class FlagActionView(FlagDecisionView):
    action = "action"


class FlagDismissView(FlagDecisionView):
    action = "dismiss"


class _RestoreView(StaffView):
    """The shared shape of a staff restore: undo a moderation takedown that was a mistake.

    The reason is required, as for every staff action that reverses another's decision, and is audited by length
    like the other decisions. Errors are checked in this order: 404 unknown id, 422 `reason_required`, 409
    `not_hidden`. (Final review #5: an id that names nothing is "no such thing" whatever the body says, so the reason
    check only ever talks about something that exists.) Subclasses name the model, the restore function and what
    they answer; the order and the audit line live here once, so a third restore cannot drift from the first two."""
    model = None
    url_kwarg = None
    noun = ""
    not_hidden = None
    restore = None

    def answer(self, obj):
        raise NotImplementedError

    def post(self, request, **kwargs):
        obj = self.model.objects.filter(pk=kwargs[self.url_kwarg]).first()
        if obj is None:
            return Response({"error": {"code": "not_found", "message": f"No such {self.noun}."}}, status=404)
        reason = request.data.get("reason") if isinstance(request.data, dict) else None
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            return Response({"error": {"code": "reason_required",
                                       "message": f"Say why the {self.noun} is being restored."}}, status=422)
        try:
            obj = self.restore(obj, admin_account_for(request), reason)
        except self.not_hidden:
            return Response({"error": {"code": "not_hidden", "message": f"That {self.noun} isn't hidden."}}, status=409)
        # The audit middleware reads the Django request, and a DRF `Request` doesn't forward attribute writes to it,
        # so `request._audit_body = ...` never reaches it; set it on the wrapped request so `notes_len` is recorded.
        request._request._audit_body = {"notes": reason}
        request._request._audit_extra = {"restored": self.noun}
        telemetry.restored(self.noun)
        return Response(self.answer(obj))


class ReportRestoreView(_RestoreView):
    """POST /admin-api/reports/{report_id}/restore {"reason"} · U1.

    A takedown (C13) is one-way for everyone but staff; this is how a mistaken one is undone. Un-hiding is all it does
    to the public surface: the report is back on the map and in public detail, and the rescuer who lost a claim to the
    takedown is not told, so a reopened report is simply claimable again."""
    model, url_kwarg, noun = StrayReport, "report_id", "report"
    not_hidden, restore = ReportNotHidden, staticmethod(restore_report)

    def answer(self, report):
        return {"report_id": str(report.pk), "status": report.status, "hidden": report.hidden_at is not None}


class ListingRestoreView(_RestoreView):
    """POST /admin-api/listings/{listing_id}/restore {"reason"} · AD7.

    The way back for a mistaken listing takedown. It only un-hides: the inquiries the takedown closed stay closed and
    nobody is notified."""
    model, url_kwarg, noun = AdoptionListing, "listing_id", "listing"
    not_hidden, restore = ListingNotHidden, staticmethod(restore_listing)

    def answer(self, listing):
        return {"listing_id": str(listing.pk), "status": listing.status, "hidden": listing.hidden_at is not None}


class StoryRestoreView(_RestoreView):
    """POST /admin-api/stories/{story_id}/restore {"reason"} · review step 4. It only un-hides: the author is not told."""
    model, url_kwarg, noun = StoryPost, "story_id", "story"
    not_hidden, restore = StoryNotHidden, staticmethod(restore_story)

    def answer(self, story):
        return {"story_id": str(story.pk), "status": story.status, "hidden": story.status == StoryStatus.HIDDEN}
