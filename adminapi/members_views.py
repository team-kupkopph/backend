"""Members: queue, record, suspend and reinstate (US-E1/E2)."""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.response import Response

from accounts.models import Account, AccountStatus, AccountType
from adminapi.pagination import decode_cursor, paginate
from adminapi.permissions import admin_account_for
from adminapi.verifications_views import StaffView
from moderation.models import ModerationFlag
from verifications.models import AccountCapability, VerificationRequest


def member_row(account):
    """⚠️ An ANONYMISED account is described as anonymised, never rendered as blank fields.

    US-N2's purge sweep stamps `anonymized_at` and clears the personal columns. A row of empty
    strings is indistinguishable from a bug, and a reviewer would open a ticket about data
    that was correctly destroyed under RA 10173.
    """
    anonymised = account.anonymized_at is not None
    return {
        "account_id": str(account.account_id),
        "display_name": "(anonymised)" if anonymised else account.display_name,
        "email": "(anonymised)" if anonymised else account.email,
        "account_type": account.account_type,
        "status": account.status,
        "created_at": account.created_at.isoformat(),
        # Both surfaced so the console can show WHERE in the §12.7 lifecycle an account sits:
        # deleted_at opens the grace window, anonymized_at closes it.
        "deleted_at": account.deleted_at.isoformat() if account.deleted_at else None,
        "anonymized_at": account.anonymized_at.isoformat() if anonymised else None,
    }


def member_detail(account):
    capabilities = list(AccountCapability.objects.filter(account=account))
    verifications = list(VerificationRequest.objects.filter(account=account).order_by("-submitted_at"))
    # Flags raised ABOUT this member — the context a reviewer needs before suspending.
    flags = list(ModerationFlag.objects.filter(target_type="account", target_id=account.account_id)
                 .order_by("-created_at")[:20])
    return {
        **member_row(account),
        "sessions_revoked_at": (account.sessions_revoked_at.isoformat()
                                if account.sessions_revoked_at else None),
        "capabilities": [{"capability": c.capability, "status": c.status,
                          "granted_at": c.granted_at.isoformat() if c.granted_at else None}
                         for c in capabilities],
        "verifications": [{"verification_id": str(v.verification_id), "type": v.type,
                           "status": v.status, "submitted_at": v.submitted_at.isoformat()}
                          for v in verifications],
        "flags_about": [{"flag_id": str(f.flag_id), "reason": f.reason, "status": f.status,
                         "created_at": f.created_at.isoformat()} for f in flags],
    }


class MemberQueueView(StaffView):
    """GET /admin-api/members?status=&type=&q=&cursor="""

    def get(self, request):
        status = request.query_params.get("status", "all")
        account_type = request.query_params.get("type")
        query = (request.query_params.get("q") or "").strip()

        # ⚠️ `admin` accounts are EXCLUDED. They are not members — they are the staff-bridge
        # rows StaffProfile points at (one per Kupkop reviewer), and they are managed on the
        # Team & roles screen (US-T1). Listing them here would duplicate that surface and, worse,
        # invite someone to suspend a reviewer from the members queue, where none of the
        # staff-account rules apply.
        qs = Account.objects.exclude(account_type=AccountType.ADMIN)
        if status != "all":
            if status not in AccountStatus.values:
                return Response({"error": {"code": "invalid_status"}}, status=400)
            qs = qs.filter(status=status)
        if account_type:
            if account_type not in AccountType.values:
                return Response({"error": {"code": "invalid_type"}}, status=400)
            qs = qs.filter(account_type=account_type)
        if query:
            qs = qs.filter(Q(display_name__icontains=query) | Q(email__icontains=query))

        # ⚠️ Soft-deleted accounts are NOT hidden. A `deleted` account inside the §12.7 grace
        # window is still real data a reviewer may legitimately need — filtering them out by
        # default would make them invisible exactly while they are still recoverable.
        qs = qs.order_by("-created_at", "account_id")
        rows, next_cursor = paginate(qs, decode_cursor(request.query_params.get("cursor", "")),
                                     ["-created_at", "account_id"])
        return Response({"results": [member_row(a) for a in rows], "next_cursor": next_cursor})


class MemberDetailView(StaffView):
    def get(self, request, account_id):
        account = Account.objects.filter(account_id=account_id).first()
        if account is None:
            return Response({"error": {"code": "not_found"}}, status=404)
        return Response(member_detail(account))


class SuspendView(StaffView):
    """POST /admin-api/members/{id}/suspend {reason}"""

    def post(self, request, account_id):
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            # An unexplained suspension is unreviewable later, and suspension is the action
            # most likely to be questioned.
            return Response({"error": {"code": "reason_required"}}, status=422)

        with transaction.atomic():
            account = Account.objects.select_for_update().filter(account_id=account_id).first()
            if account is None:
                return Response({"error": {"code": "not_found"}}, status=404)
            if account.status == AccountStatus.DELETED:
                # ⚠️ Suspending a deleted account would need deleted_at and status to disagree,
                # which the M5 CHECK constraint refuses at the database. Refuse it here with a
                # readable reason rather than letting Postgres raise.
                return Response({"error": {"code": "account_deleted"}}, status=409)
            if account.status == AccountStatus.SUSPENDED:
                return Response({"error": {"code": "already_suspended"}}, status=409)

            account.status = AccountStatus.SUSPENDED
            # ⚠️ WITHOUT THIS THE SUSPENSION IS COSMETIC. The account reads suspended while its
            # live access token keeps working for up to 15 more minutes. `sessions_revoked_at`
            # exists for exactly this: AccountJWTAuthentication rejects any token issued before
            # it. The status column is the label; this is the lock.
            account.sessions_revoked_at = timezone.now()
            # deleted_at is deliberately untouched — suspended and deleted are different states
            # and the M5 constraint rejects the pair if they are confused.
            account.save(update_fields=["status", "sessions_revoked_at"])

        request._audit_body = {"reason": reason}
        return Response(member_detail(account))


class ReinstateView(StaffView):
    """POST /admin-api/members/{id}/reinstate {reason}"""

    def post(self, request, account_id):
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"error": {"code": "reason_required"}}, status=422)

        with transaction.atomic():
            account = Account.objects.select_for_update().filter(account_id=account_id).first()
            if account is None:
                return Response({"error": {"code": "not_found"}}, status=404)
            if account.status != AccountStatus.SUSPENDED:
                return Response({"error": {"code": "not_suspended", "status": account.status}},
                                status=409)
            account.status = AccountStatus.ACTIVE
            # sessions_revoked_at is left ALONE. The revocation already happened; clearing it
            # would retroactively re-validate tokens minted before the suspension. Signing in
            # again is the correct cost.
            account.save(update_fields=["status"])

        request._audit_body = {"reason": reason}
        return Response(member_detail(account))
