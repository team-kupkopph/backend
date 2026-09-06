"""Audit middleware for /admin-api/* (US-D1).

⚠️ MIDDLEWARE, NOT PER VIEW. A per-view call is a line someone forgets, and the one they
forget is the one that mattered. Every request to the surface is recorded here, success or
failure, whether or not the view it hit exists.
"""
import re

from adminapi.models import AdminAuditLog

PREFIX = "/admin-api/"

# What the `detail` column may carry. An ALLOW-LIST: a deny-list of secrets fails open on
# the next field somebody adds, and the field nobody thought to deny is the one that leaks.
# `password`, `code`, `refresh`, `access` and `challenge` are absent by construction, not by
# being listed — they simply are not on this list.
DETAIL_FIELDS = ("status", "type", "q", "cursor", "notes_len")

# Path -> a stable action name. Ids are collapsed so the action is groupable; the id itself
# goes to target_id, where it belongs.
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _action_and_target(path: str):
    trimmed = path[len(PREFIX):].strip("/")
    found = UUID_RE.search(trimmed)
    target_id = found.group(0) if found else None
    action = UUID_RE.sub("{id}", trimmed) or "root"
    target_type = action.split("/")[0] or "root"
    return action, target_type, target_id


def _detail(request):
    """Request-shaped context only — never credential material, never document contents."""
    out = {}
    for key in DETAIL_FIELDS:
        value = request.GET.get(key)
        if value:
            out[key] = value[:120]
    # The LENGTH of a decision note is useful ("was a reason given?"); the text is the
    # applicant's and belongs on the request row, not duplicated into an audit table.
    body = getattr(request, "_audit_body", None)
    if isinstance(body, dict) and "notes" in body:
        out["notes_len"] = len(str(body.get("notes") or ""))
    return out or None


class AdminApiAuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(PREFIX):
            return self.get_response(request)

        # Read the body BEFORE the view consumes it, and keep only what _detail allows.
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                import json
                request._audit_body = json.loads(request.body or b"{}")
            except Exception:
                request._audit_body = None

        response = self.get_response(request)

        action, target_type, target_id = _action_and_target(request.path)
        user = getattr(request, "user", None)
        account = getattr(user, "admin_account", None)
        label = "anonymous"
        if account is not None:
            label = getattr(user, "username", None) or str(account.account_id)
        elif isinstance(getattr(request, "_audit_body", None), dict):
            # An unauthenticated call — a failed login is the important case. The submitted
            # identifier is what makes the row useful; the password never reaches here.
            label = str(request._audit_body.get("email") or "anonymous")[:150]

        try:
            AdminAuditLog.objects.create(
                actor=account,
                actor_label=label[:150],
                action=action[:80],
                target_type=target_type[:40],
                target_id=target_id,
                outcome=("ok" if response.status_code < 400 else f"error_{response.status_code}")[:20],
                detail=_detail(request),
            )
        except Exception:
            # ⚠️ Auditing must never take the surface down. A failure to record is a
            # monitoring problem; refusing the reviewer's decision because the log write
            # failed would turn an observability gap into an outage.
            pass

        return response
