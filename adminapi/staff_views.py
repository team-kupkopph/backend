"""Team and roles (US-T1). Superadmin only.

Replaces the `auth.User` / `auth.Group` / TOTP-device model admins, which are three of the
five registrations US-X1 demands a console route for.
"""
from django.contrib.auth.models import Group, User
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.response import Response

from accounts.models import Account, StaffProfile
from adminapi.permissions import IsSuperadmin, StaffJWTAuthentication
from adminapi.verifications_views import StaffView

ROLES = ("reviewer", "superadmin")


class SuperadminView(StaffView):
    authentication_classes = [StaffJWTAuthentication]
    permission_classes = [IsSuperadmin]


def staff_row(user):
    profile = getattr(user, "staff_profile", None)
    return {
        "id": user.id,
        "email": user.email or user.username,
        "display_name": profile.account.display_name if profile else "",
        "role": "superadmin" if user.groups.filter(name="superadmin").exists() else "reviewer",
        "is_active": user.is_active,
        # ⚠️ Surfaced because a staffer with no confirmed device cannot sign in, and on cutover
        # day that is indistinguishable from "the console is broken" unless someone can see it
        # here first.
        "totp_enrolled": TOTPDevice.objects.filter(user=user, confirmed=True).exists(),
        # A staffer with no StaffProfile is refused a token (US-B1). Showing it makes that
        # setup error visible before they hit it.
        "staff_profile_linked": profile is not None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
    }


def _superadmin_count(exclude_id=None):
    qs = User.objects.filter(is_active=True, is_staff=True, groups__name="superadmin")
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    return qs.count()


class StaffListView(SuperadminView):
    def get(self, request):
        users = (User.objects.filter(is_staff=True)
                 .select_related("staff_profile__account")
                 .prefetch_related("groups").order_by("email", "id"))
        return Response({"results": [staff_row(u) for u in users]})

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        display_name = (request.data.get("display_name") or "").strip()
        role = request.data.get("role") or "reviewer"
        if not email or not display_name:
            return Response({"error": {"code": "email_and_name_required"}}, status=422)
        if role not in ROLES:
            return Response({"error": {"code": "invalid_role"}}, status=422)
        if User.objects.filter(username=email).exists() or Account.objects.filter(email=email).exists():
            return Response({"error": {"code": "already_exists"}}, status=409)

        # ⚠️ ALL THREE ROWS OR NONE. auth.User + Account(admin) + StaffProfile joining them.
        # Any two without the third is exactly the setup error accounts/staff.py warns about,
        # and US-B1 refuses to mint a token for it — so a partial create produces a staffer who
        # cannot sign in, with no message explaining why.
        with transaction.atomic():
            user = User.objects.create_user(username=email, email=email, is_staff=True)
            user.set_unusable_password()   # they set one via the reset flow; none is mailed
            user.save(update_fields=["password"])
            account = Account.objects.create(account_type="admin", email=email,
                                             display_name=display_name)
            StaffProfile.objects.create(user=user, account=account)
            user.groups.add(Group.objects.get(name=role))

        return Response(staff_row(user), status=201)


class StaffDetailView(SuperadminView):
    def patch(self, request, user_id):
        acting_user_id = request.user.id
        user = User.objects.filter(id=user_id, is_staff=True).prefetch_related("groups").first()
        if user is None:
            return Response({"error": {"code": "not_found"}}, status=404)

        role = request.data.get("role")
        is_active = request.data.get("is_active")

        if role is not None:
            if role not in ROLES:
                return Response({"error": {"code": "invalid_role"}}, status=422)
            # ⚠️ A superadmin cannot demote THEMSELVES. Locking every human out of the only ops
            # surface is unrecoverable from inside the product — and after US-X2 there is no
            # Django admin left to recover from.
            if user.id == acting_user_id and role != "superadmin":
                return Response({"error": {"code": "cannot_demote_self"}}, status=409)
            if (role != "superadmin"
                    and user.groups.filter(name="superadmin").exists()
                    and _superadmin_count(exclude_id=user.id) == 0):
                return Response({"error": {"code": "last_superadmin"}}, status=409)

        if is_active is False:
            if user.id == acting_user_id:
                return Response({"error": {"code": "cannot_deactivate_self"}}, status=409)
            if (user.groups.filter(name="superadmin").exists()
                    and _superadmin_count(exclude_id=user.id) == 0):
                return Response({"error": {"code": "last_superadmin"}}, status=409)

        with transaction.atomic():
            if role is not None:
                user.groups.clear()
                user.groups.add(Group.objects.get(name=role))
            if is_active is not None:
                # ⚠️ Deactivation, never deletion. verification_request.reviewed_by and
                # admin_audit_log.actor_id must stay resolvable — a decision whose reviewer
                # vanished is an audit trail with a hole in it.
                user.is_active = bool(is_active)
                user.save(update_fields=["is_active"])

        user.refresh_from_db()
        return Response(staff_row(user))


class StaffResetTotpView(SuperadminView):
    """Clear a lost authenticator so the staffer can enrol a new one."""

    def post(self, request, user_id):
        user = User.objects.filter(id=user_id, is_staff=True).first()
        if user is None:
            return Response({"error": {"code": "not_found"}}, status=404)
        deleted, _ = TOTPDevice.objects.filter(user=user).delete()
        # ⚠️ Clears the device ONLY. It never reveals or sets a password — a single action that
        # did both would be a complete account takeover in one superadmin click.
        return Response({**staff_row(user), "devices_cleared": deleted})
