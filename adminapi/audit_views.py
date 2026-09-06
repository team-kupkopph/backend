"""US-W3 · the audit log, readable in the product. Superadmin only.

The model browser already exposes `admin_audit_log` as a table (US-V1). This adds what a
table cannot: filtering by actor, action and date, which is how the log is actually used —
"what did this reviewer do last Tuesday?" rather than "show me every row ever written".
"""
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from rest_framework.response import Response

from adminapi.models import AdminAuditLog
from adminapi.pagination import decode_cursor, paginate
from adminapi.permissions import IsSuperadmin, StaffJWTAuthentication
from adminapi.verifications_views import StaffView


class AuditLogView(StaffView):
    """GET /admin-api/audit?actor=&action=&from=&to=&outcome=&cursor="""

    authentication_classes = [StaffJWTAuthentication]
    permission_classes = [IsSuperadmin]

    def get(self, request):
        qs = AdminAuditLog.objects.select_related("actor")

        actor = (request.query_params.get("actor") or "").strip()
        if actor:
            # Matches the label as well as the linked account, so a FAILED LOGIN — which has
            # no actor row at all — is still findable by the address that was tried. Those are
            # the rows a security review most wants.
            qs = qs.filter(Q(actor_label__icontains=actor)
                           | Q(actor__display_name__icontains=actor)
                           | Q(actor__email__icontains=actor))

        action = (request.query_params.get("action") or "").strip()
        if action:
            qs = qs.filter(action__icontains=action)

        outcome = (request.query_params.get("outcome") or "").strip()
        if outcome == "errors":
            qs = qs.exclude(outcome="ok")
        elif outcome == "ok":
            qs = qs.filter(outcome="ok")

        for param, lookup in (("from", "created_at__gte"), ("to", "created_at__lte")):
            raw = request.query_params.get(param)
            if raw:
                parsed = parse_datetime(raw)
                if parsed is None:
                    return Response({"error": {"code": f"invalid_{param}"}}, status=400)
                qs = qs.filter(**{lookup: parsed})

        # Newest first — unlike the review queues. An audit log is read backwards from "what
        # just happened", not worked through as a backlog.
        qs = qs.order_by("-created_at", "-audit_id")
        rows, next_cursor = paginate(qs, decode_cursor(request.query_params.get("cursor", "")),
                                     ["-created_at", "-audit_id"])
        return Response({
            "results": [{
                "audit_id": str(r.audit_id),
                "actor_label": r.actor_label,
                "actor_id": str(r.actor_id) if r.actor_id else None,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": str(r.target_id) if r.target_id else None,
                "outcome": r.outcome,
                "detail": r.detail,
                "created_at": r.created_at.isoformat(),
            } for r in rows],
            "next_cursor": next_cursor,
        })
