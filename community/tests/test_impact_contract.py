"""IB26 · GET /me/impact has a contract: a serializer defines the shape, and the app's types are held to it.

The payload was a hand-built dict with no serializer and the client read it as `any`, so a renamed or
dropped field would have drawn an empty grid and called it "No badges yet". Now one place says what the
response is (`MeImpactSerializer`), the response is checked against it, and a test reads the app's
TypeScript types and compares their keys with it in both directions. That last part skips in a
backend-only checkout (see common/tests/mobile_types.py).
"""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from common.tests import mobile_types
from community.models import AccountBadge, Badge
from community.serializers import (
    BadgeProgressSerializer,
    ImpactBadgeSerializer,
    ImpactCountsSerializer,
    MeImpactSerializer,
)
from listings.tests.adoption_helpers import c
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup


def _impact(account):
    return c(account).get("/api/v1/me/impact")


def _with_shifts(account, n):
    shelter = AccountFactory(account_type="shelter")
    now = timezone.now()
    for i in range(n):
        shift = VolunteerShift.objects.create(
            shelter_account=shelter, starts_at=now - timezone.timedelta(days=i + 1),
            ends_at=now - timezone.timedelta(days=i + 1) + timezone.timedelta(hours=2))
        VolunteerSignup.objects.create(shift=shift, volunteer_account=account,
                                       status=SignupStatus.COMPLETED, waiver_accepted=True)


# ── the response is its serializer ──────────────────────────────
@pytest.mark.django_db
def test_the_response_is_valid_against_its_own_contract():
    account = AccountFactory()
    _with_shifts(account, 3)
    body = _impact(account).json()
    contract = MeImpactSerializer(data=body)
    assert contract.is_valid(), contract.errors


@pytest.mark.django_db
def test_the_response_is_valid_for_someone_with_nothing():
    contract = MeImpactSerializer(data=_impact(AccountFactory()).json())
    assert contract.is_valid(), contract.errors


@pytest.mark.django_db
def test_the_response_is_valid_for_someone_with_everything():
    account = AccountFactory()
    for badge in Badge.objects.all():
        AccountBadge.objects.create(account=account, badge=badge)
    body = _impact(account).json()
    contract = MeImpactSerializer(data=body)
    assert contract.is_valid(), contract.errors
    assert all(b["earned"] and b["earned_at"] for b in body["badges"])


@pytest.mark.django_db
def test_an_unearned_badge_says_so_with_a_null_date():
    body = _impact(AccountFactory()).json()
    assert all(b["earned"] is False and b["earned_at"] is None for b in body["badges"])


@pytest.mark.django_db
def test_earned_at_is_utc_with_a_z():
    """`isoformat()` wrote +00:00; the serializer's DateTimeField writes Z. The same instant, and the app's
    `new Date()` reads both: pinned so a change is a decision, not a surprise."""
    account = AccountFactory()
    AccountBadge.objects.create(account=account, badge=Badge.objects.get(pk="first_shift"))
    earned = {b["badge_code"]: b for b in _impact(account).json()["badges"]}["first_shift"]
    assert earned["earned_at"].endswith("Z")


@pytest.mark.django_db
def test_a_badge_with_blank_text_still_serializes():
    Badge.objects.create(badge_code="plain", name="Plain", description="", icon="", criteria="",
                         metric="shifts_completed", threshold=99, sort_order=99)
    body = _impact(AccountFactory()).json()
    plain = {b["badge_code"]: b for b in body["badges"]}["plain"]
    assert plain["description"] == "" and plain["icon"] == "" and plain["criteria"] == ""


@pytest.mark.django_db
def test_a_badge_with_no_rule_serializes_with_null_progress():
    Badge.objects.create(badge_code="founder", name="Founder", criteria="Here at the start")
    body = _impact(AccountFactory()).json()
    assert {b["badge_code"]: b for b in body["badges"]}["founder"]["progress"] is None


@pytest.mark.django_db
def test_signed_out_is_a_401_not_a_payload():
    from rest_framework.test import APIClient
    res = APIClient().get("/api/v1/me/impact")
    assert res.status_code == 401 and "badges" not in res.json()


# ── the contract refuses a bad shape ────────────────────────────
@pytest.mark.parametrize("bad", [
    {"impact": None, "badges": []},
    {"impact": {"shifts_completed": 1, "rescues_helped": 0, "pets_rehomed": 0, "pledges_delivered": 0}},
    {"impact": {"shifts_completed": 1, "rescues_helped": 0, "pets_rehomed": 0, "pledges_delivered": 0}, "badges": None},
    {"impact": {"shifts_completed": "many", "rescues_helped": 0, "pets_rehomed": 0, "pledges_delivered": 0}, "badges": []},
    {"impact": {"shifts_completed": 1, "rescues_helped": 0, "pets_rehomed": 0, "pledges_delivered": 0},
     "badges": [{"badge_code": "x"}]},
])
def test_the_contract_rejects_a_payload_that_is_not_the_shape(bad):
    assert not MeImpactSerializer(data=bad).is_valid()


def test_progress_needs_both_numbers():
    assert not BadgeProgressSerializer(data={"current": 3}).is_valid()
    assert BadgeProgressSerializer(data={"current": 3, "target": 10}).is_valid()


# ── the app's types are held to it (skips without mobile_app/) ──
needs_mobile = pytest.mark.skipif(
    not mobile_types.available("community.ts"),
    reason="mobile_app not checked out beside backend/ (set KUPKOP_MOBILE_APP)")
TYPE_FILES = ("community.ts", "navigation/types.ts")     # BadgeShape moved from the second to the first


@needs_mobile
def test_the_apps_impact_response_has_the_servers_two_blocks():
    assert mobile_types.ts_keys("ImpactResponse", *TYPE_FILES) == set(MeImpactSerializer().fields)


@needs_mobile
def test_the_apps_impact_counts_are_the_servers():
    assert mobile_types.ts_keys("Impact", *TYPE_FILES) == set(ImpactCountsSerializer().fields)


@needs_mobile
def test_the_apps_badge_is_the_servers():
    assert mobile_types.ts_keys("BadgeShape", *TYPE_FILES) == set(ImpactBadgeSerializer().fields)


@needs_mobile
def test_the_apps_progress_is_the_servers():
    assert mobile_types.ts_keys("BadgeProgress", *TYPE_FILES) == set(BadgeProgressSerializer().fields)
