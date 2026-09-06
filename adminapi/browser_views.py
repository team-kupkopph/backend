"""The read-only model browser (US-V1). Superadmin only.

⚠️ READ-ONLY, AND NOT STUBBED. There is no write path here and none is scaffolded for later.
A generic form that writes any row bypasses exactly the logic the console's architecture
protects: setting `verification_request.status` directly approves a shelter without firing
notify() or stamping reviewed_by, leaving the shelter approved and never told. A test asserts
this module's routes register no POST, PATCH, PUT or DELETE.
"""
from django.core.exceptions import FieldError
from django.db.models import Q
from rest_framework.response import Response

from accounts.staff import reviewer_account
from adminapi.browser import MAX_PAGE, registry, resolve
from adminapi.pagination import decode_cursor, paginate
from adminapi.permissions import IsSuperadmin, StaffJWTAuthentication, admin_account_for
from adminapi.verifications_views import StaffView
from verifications.models import VerificationAccessLog


class BrowserView(StaffView):
    authentication_classes = [StaffJWTAuthentication]
    permission_classes = [IsSuperadmin]


def serialise(obj, fields):
    out = {}
    for name in fields:
        value = getattr(obj, name, None)
        out[name] = value if value is None or isinstance(value, (str, int, float, bool)) else str(value)
    return out


def _log_document_access(request, model, rows):
    """⚠️ The browser is not a way around the access log.

    Reading `verification_document` rows here is reading about someone's government ID, so it
    writes the same VerificationAccessLog row the review screen does (US-SEC3 / RA 10173).
    """
    if (model._meta.app_label, model._meta.object_name) != ("verifications", "VerificationDocument"):
        return
    viewer = admin_account_for(request) or reviewer_account(request.user)
    VerificationAccessLog.objects.bulk_create([
        VerificationAccessLog(verification_id=row.verification_id, viewer=viewer,
                              staff_username=request.user.get_username())
        for row in rows
    ])


class ModelListView(BrowserView):
    """GET /admin-api/models — what may be browsed at all."""

    def get(self, request):
        return Response({"results": registry()})


class ModelRowsView(BrowserView):
    """GET /admin-api/models/{app}.{model}?q=&cursor="""

    def get(self, request, key):
        model, fields = resolve(key)
        if model is None:
            # Absent from the allow-list is indistinguishable from absent entirely, on purpose.
            return Response({"error": {"code": "not_browsable"}}, status=404)

        qs = model.objects.all()
        query = (request.query_params.get("q") or "").strip()
        if query:
            # Search only across allow-listed TEXT fields — never a field that is not returned,
            # or the browser would become an oracle for values it refuses to show.
            predicate = Q()
            matched = False
            for name in fields:
                try:
                    field = model._meta.get_field(name)
                except Exception:
                    continue
                if field.get_internal_type() in ("CharField", "TextField"):
                    predicate |= Q(**{f"{name}__icontains": query})
                    matched = True
            if matched:
                qs = qs.filter(predicate)

        pk = model._meta.pk.name
        try:
            qs = qs.order_by(pk)
        except FieldError:
            return Response({"error": {"code": "not_orderable"}}, status=400)

        limit = min(int(request.query_params.get("limit") or MAX_PAGE), MAX_PAGE)
        rows, next_cursor = paginate(qs, decode_cursor(request.query_params.get("cursor", "")),
                                     [pk], page_size=limit)
        _log_document_access(request, model, rows)
        return Response({
            "key": key,
            "fields": list(fields),
            "results": [serialise(r, fields) for r in rows],
            "next_cursor": next_cursor,
        })


class ModelRowView(BrowserView):
    """GET /admin-api/models/{app}.{model}/{pk}"""

    def get(self, request, key, pk):
        model, fields = resolve(key)
        if model is None:
            return Response({"error": {"code": "not_browsable"}}, status=404)
        row = model.objects.filter(pk=pk).first()
        if row is None:
            return Response({"error": {"code": "not_found"}}, status=404)
        _log_document_access(request, model, [row])
        return Response({"key": key, "fields": list(fields), "row": serialise(row, fields)})
