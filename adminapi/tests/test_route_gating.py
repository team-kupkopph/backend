"""US-B3 · every /admin-api/* route is authenticated and gated.

⚠️ This test DERIVES the route list from the URLconf. It never hard-codes one.

That is the rule this project paid for five times in Sprint 8: a hand-written list said 5
when the truth was 42. A gating test with a hard-coded list protects the routes someone
remembered to add to it — which are never the ones that got forgotten.
"""
import pytest
from django.urls import get_resolver

from adminapi.permissions import IsStaffJWT, IsSuperadmin, StaffJWTAuthentication
from adminapi.tests.conftest import full_signin

# Routes that are unauthenticated BY DESIGN: you cannot present a staff token before you have
# one. Capped and named, so a sixth public route is a deliberate edit to this list.
PUBLIC = {
    "admin-api/auth/login",
    "admin-api/auth/verify-otp",
    # US-T1 · first-run enrolment. Password-gated inside the view (start_enrolment
    # re-authenticates), and the device it creates is UNCONFIRMED, so it satisfies no second
    # factor until a code proves it.
    "admin-api/auth/enrol-totp",
    "admin-api/auth/confirm-totp",
    "admin-api/auth/refresh",
    "admin-api/auth/password-reset",
    "admin-api/auth/password-reset/confirm",
}


def admin_api_views():
    """Every view callable mounted under /admin-api/, derived from the URLconf."""
    found = []
    for pattern in get_resolver().url_patterns:
        prefix = str(getattr(pattern, "pattern", ""))
        if prefix != "admin-api/":
            continue
        for sub in pattern.url_patterns:
            found.append((prefix + str(sub.pattern), sub.callback.cls))
    return found


def test_the_scan_found_routes():
    """A zero here must never read as 'all routes are gated'."""
    routes = admin_api_views()
    assert len(routes) >= 6, f"expected the adminapi URLconf to expose routes, found {routes}"


def test_every_non_public_route_uses_staff_authentication():
    offenders = [
        path for path, cls in admin_api_views()
        if path not in PUBLIC and StaffJWTAuthentication not in cls.authentication_classes
    ]
    # Without this, a view inherits DRF's global AccountJWTAuthentication (settings.py) and is
    # authenticated by the MOBILE authenticator, which cannot read a staff token at all.
    assert not offenders, f"route(s) not using StaffJWTAuthentication: {offenders}"


def test_every_non_public_route_is_permission_gated():
    offenders = [
        path for path, cls in admin_api_views()
        if path not in PUBLIC
        and not ({IsStaffJWT, IsSuperadmin} & set(cls.permission_classes))
    ]
    assert not offenders, f"route(s) with no staff permission: {offenders}"


def test_public_routes_are_all_real():
    """Ratchet: a name left in PUBLIC after its route is gone would silently exempt nothing —
    but it would also hide the next route that takes the same path."""
    paths = {path for path, _ in admin_api_views()}
    stale = PUBLIC - paths
    assert not stale, f"PUBLIC names route(s) that no longer exist: {stale}"


@pytest.mark.django_db
def test_an_unauthenticated_request_to_a_gated_route_is_refused(client):
    res = client.post("/admin-api/auth/logout", {"refresh": "x"}, content_type="application/json")
    assert res.status_code in (401, 403)


@pytest.mark.django_db
def test_the_two_role_groups_exist(db):
    from django.contrib.auth.models import Group
    assert set(Group.objects.filter(name__in=["reviewer", "superadmin"])
               .values_list("name", flat=True)) == {"reviewer", "superadmin"}


@pytest.mark.django_db
def test_a_reviewer_is_refused_a_superadmin_route(client, staffer):
    """403, not 404. Hiding the route from a reviewer is a courtesy in the UI; the refusal is
    the control, and it should say 'not allowed' rather than 'does not exist'."""
    from django.contrib.auth.models import Group
    from rest_framework.decorators import api_view, authentication_classes, permission_classes
    from rest_framework.response import Response
    from rest_framework.test import APIRequestFactory

    staffer.groups.add(Group.objects.get(name="reviewer"))
    body = full_signin(client, staffer)

    @api_view(["GET"])
    @authentication_classes([StaffJWTAuthentication])
    @permission_classes([IsSuperadmin])
    def only_superadmins(_request):
        return Response({"ok": True})

    request = APIRequestFactory().get("/x", HTTP_AUTHORIZATION=f"Bearer {body['access']}")
    assert only_superadmins(request).status_code == 403


@pytest.mark.django_db
def test_a_superadmin_passes_the_same_route(client, staffer):
    from django.contrib.auth.models import Group
    from rest_framework.decorators import api_view, authentication_classes, permission_classes
    from rest_framework.response import Response
    from rest_framework.test import APIRequestFactory

    staffer.groups.add(Group.objects.get(name="superadmin"))
    body = full_signin(client, staffer)

    @api_view(["GET"])
    @authentication_classes([StaffJWTAuthentication])
    @permission_classes([IsSuperadmin])
    def only_superadmins(_request):
        return Response({"ok": True})

    request = APIRequestFactory().get("/x", HTTP_AUTHORIZATION=f"Bearer {body['access']}")
    assert only_superadmins(request).status_code == 200
