"""The moderation queue, detail and decisions (US-M1/M2).

⚠️ `ModerationFlag` is one of the three registered model admins, so this track is what US-X1
demands before US-X2 can switch the Django admin off. It is not de-scopable.
"""
from django.apps import apps
from django.db import transaction
from rest_framework.response import Response

from adminapi.pagination import decode_cursor, paginate
from adminapi.permissions import admin_account_for
from adminapi.verifications_views import StaffView
from moderation.actions import DECIDABLE, ModerationError, resolve_flag
from moderation.models import FlagStatus, FlagTarget, ModerationFlag

# `target_type`/`target_id` is a GENERIC pointer with no foreign key — the targets span
# listings, sagip, shelter, community and accounts, so one column per type was rejected in
# favour of this. Resolution is therefore an explicit dispatch, not a join.
#
# ⚠️ `message` is absent ON PURPOSE: messaging is Phase 2 and no model exists. A flag of that
# type is describable but not resolvable, which is a different state from "the row was
# deleted" and is reported differently below.
TARGETS = {
    FlagTarget.ACCOUNT: ("accounts", "Account", lambda o: o.display_name or o.email),
    FlagTarget.LISTING: ("listings", "AdoptionListing", lambda o: getattr(o, "title", "") or str(o.pk)),
    FlagTarget.REPORT:  ("sagip", "StrayReport", lambda o: getattr(o, "description", "") or str(o.pk)),
    FlagTarget.QR:      ("shelter", "DonationQr", lambda o: str(o.pk)),
    FlagTarget.STORY:   ("community", "StoryPost", lambda o: getattr(o, "caption", "") or str(o.pk)),
}


def resolve_target(flag):
    """Describe what a flag points at, without ever raising.

    ⚠️ A missing target is NORMAL — it is often *why* something was flagged, and moderation
    frequently ends with the target gone. Rendering that as a 500 would make the queue
    unusable exactly when it matters.
    """
    spec = TARGETS.get(flag.target_type)
    if spec is None:
        return {"kind": flag.target_type, "state": "unsupported",
                "label": "This target type has no surface yet."}
    app, model, describe = spec
    try:
        obj = apps.get_model(app, model).objects.filter(pk=flag.target_id).first()
    except Exception:
        obj = None
    if obj is None:
        return {"kind": flag.target_type, "state": "gone",
                "label": "Target no longer exists."}
    try:
        label = str(describe(obj))[:200]
    except Exception:
        label = str(flag.target_id)
    return {"kind": flag.target_type, "state": "present", "label": label}


def flag_row(flag, *, with_target=False):
    return {
        "flag_id": str(flag.flag_id),
        "target_type": flag.target_type,
        "target_id": str(flag.target_id),
        "reason": flag.reason,
        "status": flag.status,
        "created_at": flag.created_at.isoformat(),
        "reviewed_at": flag.reviewed_at.isoformat() if flag.reviewed_at else None,
        # ⚠️ NULL reporter = system-raised (e.g. the repeat-withdrawal rule), not a person.
        # The column is nullable by schema accommodation; the UI must render "System" rather
        # than crash on a null.
        "reporter": (
            {"account_id": str(flag.reporter_account.account_id),
             "display_name": flag.reporter_account.display_name,
             "email": flag.reporter_account.email}
            if flag.reporter_account_id else None
        ),
        **({"target": resolve_target(flag)} if with_target else {}),
    }


class FlagQueueView(StaffView):
    """GET /admin-api/flags?status=&target_type=&cursor="""

    def get(self, request):
        status = request.query_params.get("status", FlagStatus.OPEN)
        target_type = request.query_params.get("target_type")

        qs = ModerationFlag.objects.select_related("reporter_account")
        if status != "all":
            if status not in FlagStatus.values:
                return Response({"error": {"code": "invalid_status"}}, status=400)
            qs = qs.filter(status=status)
        if target_type:
            if target_type not in FlagTarget.values:
                return Response({"error": {"code": "invalid_target_type"}}, status=400)
            qs = qs.filter(target_type=target_type)

        # Oldest first, for the same reason the verification queue is: a moderation backlog is
        # a backlog. Uses the existing idx_flag_status / idx_flag_target.
        qs = qs.order_by("created_at", "flag_id")
        rows, next_cursor = paginate(qs, decode_cursor(request.query_params.get("cursor", "")),
                                     ["created_at", "flag_id"])
        return Response({"results": [flag_row(f) for f in rows], "next_cursor": next_cursor})


class FlagDetailView(StaffView):
    def get(self, request, flag_id):
        flag = (ModerationFlag.objects.select_related("reporter_account")
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

        try:
            with transaction.atomic():
                # Same guard as US-C3: two moderators on one flag is normal, and without the
                # lock the second silently overwrites the first reviewer's stamp.
                flag = ModerationFlag.objects.select_for_update().filter(flag_id=flag_id).first()
                if flag is None:
                    return Response({"error": {"code": "not_found"}}, status=404)
                if flag.status not in DECIDABLE:
                    return Response(
                        {"error": {"code": "already_decided", "status": flag.status}}, status=409)
                flag, effects = resolve_flag(flag, admin_account_for(request), target_status)
        except ModerationError as exc:
            return Response({"error": {"code": "invalid_decision", "message": str(exc)}}, status=422)

        flag.refresh_from_db()
        return Response({**flag_row(flag, with_target=True), "side_effects": effects})


class FlagReviewView(FlagDecisionView):
    action = "review"


class FlagActionView(FlagDecisionView):
    action = "action"


class FlagDismissView(FlagDecisionView):
    action = "dismiss"
