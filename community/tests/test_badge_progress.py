"""IB5 / V7 / IB15 · a badge row carries its own rule, and /me/impact says how far along each is.

The thresholds were an `if` ladder in `_earned_codes`, the display criteria free text in a seed
migration, and nothing tied the two: a reworded criterion or a new badge drifted silently. The
rule is now `metric` + `threshold` on the badge row, so a new badge is a row, not a code change,
and the screen can draw "8 of 10" without knowing any badge by name.
"""
import pytest
from django.contrib.gis.geos import Point
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.factories import AccountFactory
from community.badges import award_badges_for, badge_progress, earned_codes
from community.models import AccountBadge, Badge
from listings.models import AdoptionListing, ListingStatus
from listings.tests.adoption_helpers import c
from sagip.models import RescueCase, StrayReport
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup

# code → (metric, threshold): the seeded catalog, as D-S6-2 and the owner decisions define it.
SEEDED = {
    "first_shift": ("shifts_completed", 1), "shifts_10": ("shifts_completed", 10),
    "shifts_50": ("shifts_completed", 50), "first_rescue": ("rescues_helped", 1),
    "rehomed_1": ("pets_rehomed", 1), "rehomed_5": ("pets_rehomed", 5),
    "bayani": ("areas_helped", 3),
}


def _shifts(account, n):
    shelter = AccountFactory(account_type="shelter")
    now = timezone.now()
    for i in range(n):
        shift = VolunteerShift.objects.create(
            shelter_account=shelter, starts_at=now - timezone.timedelta(days=i + 1),
            ends_at=now - timezone.timedelta(days=i + 1) + timezone.timedelta(hours=2))
        VolunteerSignup.objects.create(shift=shift, volunteer_account=account,
                                       status=SignupStatus.COMPLETED, waiver_accepted=True)


def _rescue(account):
    report = StrayReport.objects.create(reporter_account=AccountFactory(), species="dog",
                                        condition="injured", status="resolved",
                                        geom=Point(121.05, 14.63, srid=4326))
    RescueCase.objects.create(report=report, claimed_by_account=account,
                              resolved_at=timezone.now(), outcome_notes="Safe")


def _rehome(account):
    AdoptionListing.objects.create(posted_by=account, name="Rex", species="dog",
                                   status=ListingStatus.ADOPTED, adopted_by_account=AccountFactory())


def _impact(account):
    return c(account).get("/api/v1/me/impact").json()


def _by_code(body):
    return {b["badge_code"]: b for b in body["badges"]}


# ── the seeded rules ────────────────────────────────────────────
@pytest.mark.django_db
@pytest.mark.parametrize("code,rule", sorted(SEEDED.items()))
def test_every_seeded_badge_carries_its_rule(code, rule):
    badge = Badge.objects.get(pk=code)
    assert (badge.metric, badge.threshold) == rule


@pytest.mark.django_db
def test_the_catalog_is_in_a_deliberate_order():
    codes = list(Badge.objects.order_by("sort_order", "badge_code").values_list("pk", flat=True))
    assert codes == ["first_shift", "shifts_10", "shifts_50", "first_rescue",
                     "rehomed_1", "rehomed_5", "bayani"]


@pytest.mark.django_db
def test_a_new_badge_is_a_row_not_a_code_change():
    Badge.objects.create(badge_code="shifts_2", name="Twice", criteria="2 completed volunteer shifts",
                         metric="shifts_completed", threshold=2, sort_order=15)
    volunteer = AccountFactory()
    _shifts(volunteer, 2)
    assert "shifts_2" in award_badges_for(volunteer)


@pytest.mark.django_db
def test_a_badge_with_no_rule_is_never_awarded_by_the_system():
    Badge.objects.create(badge_code="founder", name="Founder", criteria="Here at the start")
    volunteer = AccountFactory()
    _shifts(volunteer, 60)
    assert "founder" not in award_badges_for(volunteer)


@pytest.mark.django_db
def test_a_badge_with_an_unknown_metric_is_skipped_not_fatal():
    Badge.objects.create(badge_code="odd", name="Odd", criteria="?", metric="bogus", threshold=1)
    volunteer = AccountFactory()
    _shifts(volunteer, 1)
    assert award_badges_for(volunteer) == ["first_shift"]
    assert _by_code(_impact(volunteer))["odd"]["progress"] is None


@pytest.mark.django_db
def test_the_three_areas_are_counted_for_bayani():
    def counts(**kw):
        return {"shifts_completed": 0, "rescues_helped": 0, "pets_rehomed": 0,
                "pledges_delivered": 0, **kw}

    rules = list(Badge.objects.all())
    assert "bayani" not in earned_codes(counts(shifts_completed=50, rescues_helped=9), rules)
    assert "bayani" in earned_codes(counts(shifts_completed=1, rescues_helped=1, pets_rehomed=1), rules)


# ── progress in the payload ─────────────────────────────────────
@pytest.mark.django_db
def test_progress_is_current_and_target_for_each_badge():
    volunteer = AccountFactory()
    _shifts(volunteer, 8)
    badges = _by_code(_impact(volunteer))
    assert badges["shifts_10"]["progress"] == {"current": 8, "target": 10}
    assert badges["shifts_50"]["progress"] == {"current": 8, "target": 50}
    assert badges["first_rescue"]["progress"] == {"current": 0, "target": 1}


@pytest.mark.django_db
def test_progress_is_honest_past_the_target():
    volunteer = AccountFactory()
    _shifts(volunteer, 8)
    assert _by_code(_impact(volunteer))["first_shift"]["progress"] == {"current": 8, "target": 1}


@pytest.mark.django_db
def test_bayani_progress_counts_areas_not_activities():
    volunteer = AccountFactory()
    _shifts(volunteer, 5)
    _rescue(volunteer)
    assert _by_code(_impact(volunteer))["bayani"]["progress"] == {"current": 2, "target": 3}


@pytest.mark.django_db
def test_a_held_badge_stays_earned_when_its_stat_has_fallen():
    """IQ2 / IQ3 keep held badges: the stat can fall below the target while the badge stays."""
    poster = AccountFactory()
    AccountBadge.objects.create(account=poster, badge=Badge.objects.get(pk="rehomed_1"))
    badge = _by_code(_impact(poster))["rehomed_1"]
    assert badge["earned"] is True and badge["progress"] == {"current": 0, "target": 1}


@pytest.mark.django_db
def test_progress_agrees_with_what_the_sweep_would_award():
    volunteer = AccountFactory()
    _shifts(volunteer, 10)
    _rescue(volunteer)
    _rehome(volunteer)
    body = _impact(volunteer)
    would_earn = {code for code, b in _by_code(body).items()
                  if b["progress"] and b["progress"]["current"] >= b["progress"]["target"]}
    assert set(award_badges_for(volunteer)) == would_earn


@pytest.mark.django_db
def test_progress_for_a_badge_is_a_pure_function_of_counts():
    badge = Badge.objects.get(pk="shifts_10")
    zero = {"shifts_completed": 0, "rescues_helped": 0, "pets_rehomed": 0, "pledges_delivered": 0}
    assert badge_progress(badge, zero) == {"current": 0, "target": 10}
    assert badge_progress(Badge(badge_code="x", name="x"), zero) is None


# ── order and shape ─────────────────────────────────────────────
@pytest.mark.django_db
def test_the_catalog_comes_back_in_catalog_order_not_earned_first():
    volunteer = AccountFactory()
    _shifts(volunteer, 1)
    award_badges_for(volunteer)
    codes = [b["badge_code"] for b in _impact(volunteer)["badges"]]
    assert codes == ["first_shift", "shifts_10", "shifts_50", "first_rescue",
                     "rehomed_1", "rehomed_5", "bayani"]


@pytest.mark.django_db
def test_every_badge_row_keeps_its_old_fields():
    body = _impact(AccountFactory())
    assert set(body["badges"][0]) == {"badge_code", "name", "description", "icon", "criteria",
                                      "earned", "earned_at", "progress"}
    assert set(body["impact"]) == {"shifts_completed", "rescues_helped", "pets_rehomed",
                                   "pledges_delivered"}


@pytest.mark.django_db
def test_the_impact_read_costs_a_fixed_number_of_queries():
    def cost(account):
        with CaptureQueriesContext(connection) as ctx:
            assert c(account).get("/api/v1/me/impact").status_code == 200
        return len(ctx)

    quiet, busy = AccountFactory(), AccountFactory()
    _shifts(busy, 12)
    _rescue(busy)
    _rehome(busy)
    award_badges_for(busy)
    assert cost(quiet) == cost(busy)
