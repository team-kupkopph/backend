"""The verification queue, detail and decisions (US-C1/C2/C3).

⚠️ These views are THIN. Every state change delegates to `verifications/review.py`, which is
where `notify()` fires, `reviewed_by` is stamped and the Verified-Member capability is granted.
Re-implementing any of that here would give the schema two writers that disagree — the exact
failure the console's architecture (spec §2) exists to prevent.
"""
from django.db import transaction
from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.staff import reviewer_account
from adminapi.pagination import decode_cursor, paginate
from adminapi.permissions import IsStaffJWT, StaffJWTAuthentication, admin_account_for
from adminapi.serializers import detail, queue_row
from verifications.models import (
    VerificationAccessLog,
    VerificationRequest,
    VerificationStatus,
    VerificationType,
)
from verifications.review import (
    ReviewError,
    approve_request,
    reject_request,
    request_more_info,
)

# A decision can only be made from these. Anything else has already been decided, and a second
# decision would overwrite the first reviewer's stamp with no record that it happened.
DECIDABLE = {VerificationStatus.PENDING, VerificationStatus.NEEDS_INFO}


class StaffView(APIView):
    # Both are required. permission_classes alone leaves DRF's global AccountJWTAuthentication
    # in place, which cannot read a staff token — US-B3's route test asserts this pairing.
    authentication_classes = [StaffJWTAuthentication]
    permission_classes = [IsStaffJWT]


class VerificationQueueView(StaffView):
    """GET /admin-api/verifications?status=&type=&q=&cursor="""

    def get(self, request):
        status = request.query_params.get("status", VerificationStatus.PENDING)
        vtype = request.query_params.get("type")
        query = (request.query_params.get("q") or "").strip()

        qs = VerificationRequest.objects.select_related("account")

        if status != "all":
            if status not in VerificationStatus.values:
                return Response({"error": {"code": "invalid_status"}}, status=400)
            qs = qs.filter(status=status)
        if vtype:
            if vtype not in VerificationType.values:
                return Response({"error": {"code": "invalid_type"}}, status=400)
            qs = qs.filter(type=vtype)
        if query:
            qs = qs.filter(Q(account__display_name__icontains=query)
                           | Q(account__email__icontains=query))

        # ⚠️ OLDEST FIRST, and that is a product decision, not a default. A reviewer's queue is
        # a backlog, not a feed: newest-first quietly starves the request that has been waiting
        # longest, which is the one an applicant is actually blocked on.
        #
        # Ordered by (submitted_at, verification_id) because the pair is unique — submitted_at
        # alone can tie, and a tie at a page boundary drops rows.
        qs = qs.annotate(document_count=Count("documents")).order_by("submitted_at", "verification_id")

        # ⚠️ NO INDEX ADDED, deliberately. The existing idx_verification_acct_type_st is
        # account-leading and serves the public-visibility predicate; it does not help a
        # status-filtered scan. At launch volume — tens per week — a sequential scan is free,
        # and an index added on a guess is one nobody can later justify removing. Measure with
        # real data first; the composite would be (status, submitted_at).
        rows, next_cursor = paginate(qs, decode_cursor(request.query_params.get("cursor", "")),
                                     ["submitted_at", "verification_id"])
        return Response({"results": [queue_row(v) for v in rows], "next_cursor": next_cursor})


class VerificationDetailView(StaffView):
    """GET /admin-api/verifications/{id}"""

    def get(self, request, verification_id):
        vr = (VerificationRequest.objects
              .select_related("account")
              .prefetch_related("documents")
              .filter(verification_id=verification_id)
              .first())
        if vr is None:
            return Response({"error": {"code": "not_found"}}, status=404)

        # ⚠️ Every view of a request's documents writes an access-log row (US-SEC3 / RA 10173).
        # Decisions were already attributable through reviewed_by; this covers LOOKING — a
        # reviewer can open a government ID and never decide anything. `staff_username` is
        # always captured so a view is never silently unattributed.
        VerificationAccessLog.objects.create(
            verification=vr,
            viewer=admin_account_for(request) or reviewer_account(request.user),
            staff_username=request.user.get_username(),
        )
        return Response(detail(vr))


class VerificationDecisionView(StaffView):
    """POST /admin-api/verifications/{id}/{approve|reject|needs-info}"""

    action = None

    def post(self, request, verification_id):
        reviewer = admin_account_for(request)
        notes = (request.data.get("notes") or "").strip()

        try:
            with transaction.atomic():
                # ⚠️ select_for_update + a status gate INSIDE the transaction. Two reviewers
                # opening the same request is normal, not exotic. Without the lock, both read
                # `pending`, both write, and the second silently overwrites the first
                # reviewer's stamp — the applicant gets two notifications and the audit trail
                # records one decision. Same shape as the live-verified Track H inquiry guard.
                vr = (VerificationRequest.objects
                      .select_for_update()
                      .filter(verification_id=verification_id)
                      .first())
                if vr is None:
                    return Response({"error": {"code": "not_found"}}, status=404)
                if vr.status not in DECIDABLE:
                    return Response(
                        {"error": {"code": "already_decided", "status": vr.status}}, status=409)

                if self.action == "approve":
                    approve_request(vr, reviewer)
                elif self.action == "reject":
                    reject_request(vr, reviewer, notes)
                else:
                    request_more_info(vr, reviewer, notes)
        except ReviewError as exc:
            # A rejection or needs-info with no reason. The applicant is shown this text, so an
            # empty one is a dead end they cannot act on.
            return Response({"error": {"code": "reason_required", "message": str(exc)}}, status=422)

        vr.refresh_from_db()
        return Response(detail(vr))


class ApproveView(VerificationDecisionView):
    action = "approve"


class RejectView(VerificationDecisionView):
    action = "reject"


class NeedsInfoView(VerificationDecisionView):
    action = "needs_info"
