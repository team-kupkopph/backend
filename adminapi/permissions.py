"""Staff request authentication and the role gate (US-B1 / US-B3)."""
from django.contrib.auth.models import User
from rest_framework.permissions import BasePermission
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

from accounts.models import Account, StaffProfile


class StaffJWTAuthentication(JWTAuthentication):
    """Resolve a staff token to its Django `User`, with the admin `Account` attached.

    Mirrors `accounts/authentication.py::AccountJWTAuthentication`, but for the other
    identity. The two never mix: an Account token has no `staff_user_id`, so it cannot
    authenticate here, and a staff token has no `account_id`, so it cannot authenticate
    there. That mutual rejection is asserted by tests, because "the mobile token happens to
    work on the admin API" is the kind of hole nobody notices until it is exploited.
    """

    def get_user(self, validated_token):
        staff_user_id = validated_token.get("staff_user_id")
        admin_account_id = validated_token.get("admin_account_id")
        if staff_user_id is None or admin_account_id is None:
            raise InvalidToken("not_a_staff_token")

        user = User.objects.filter(id=staff_user_id, is_active=True, is_staff=True).first()
        if user is None:
            raise InvalidToken("staff_user_not_found")

        # Re-resolve the bridge on EVERY request rather than trusting the claim. A staffer
        # whose StaffProfile was removed (offboarding) must stop being able to record
        # decisions immediately, not when their 8-hour refresh happens to expire.
        profile = StaffProfile.objects.filter(user=user).select_related("account").first()
        if profile is None or str(profile.account.account_id) != str(admin_account_id):
            raise InvalidToken("staff_profile_missing")

        user.admin_account = profile.account
        return user


class IsStaffJWT(BasePermission):
    """Every /admin-api/* route carries this. A test enumerates the URLconf to prove it."""

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and getattr(user, "admin_account", None))


class IsSuperadmin(BasePermission):
    """Superadmin-only routes (team management, the model browser).

    Roles are Django **Groups**, not a `staff_profile` column: `check_schema_vs_migrations`
    scans project migrations only and skips `.venv`, so `auth_group` costs nothing while a new
    column would drag in the full Tech-Spec-§7 -> schema.sql -> migration parity cycle.
    """

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated and getattr(user, "admin_account", None)):
            return False
        return user.groups.filter(name="superadmin").exists()


def admin_account_for(request) -> Account | None:
    """The `Account` a request's decisions are attributed to."""
    return getattr(request.user, "admin_account", None)
