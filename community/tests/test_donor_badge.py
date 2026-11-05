"""IB12 / IQ5 · a donor can earn a badge: "Generous", on a pledge a shelter confirms it received.

The impact screen counted delivered pledges ("Donations") but no badge used them, so someone who helps by giving was
the one kind of helper with nothing to earn (D-S6-2's principle: nobody who helps should be unbadgeable). Owner
decision 2026-11-05 (IQ5): yes. The evidence is the SHELTER confirming receipt, a second party who is an approved
shelter, so a pledge that was only made, or that the donor cancelled, earns nothing.
"""
from unittest import mock

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Account, AccountStatus
from community.badges import award_badges_for
from community.models import AccountBadge, Badge, NeedPledge, PledgeStatus
from community.sweeps import _candidate_account_ids, award_badges
from community.tests.test_needs import _hdr, _need, _pledge, _shelter
from notifications.models import Notification


def _held(account):
    return set(AccountBadge.objects.filter(account=account).values_list("badge_id", flat=True))


def _confirm(client, shelter, need, pledge, quantity=1):
    return client.post(f"/api/v1/needs/{need.pk}/received", {"pledge_id": str(pledge.pk), "quantity_received": quantity},
                       content_type="application/json", **_hdr(shelter))


def _delivered(donor):
    need = _need()
    return NeedPledge.objects.create(need=need, pledger_account=donor, quantity=1, status=PledgeStatus.DELIVERED,
                                     quantity_received=1, decided_at=timezone.now())


# ── the badge row ───────────────────────────────────────────────
@pytest.mark.django_db
@pytest.mark.parametrize("field,expected", [
    ("name", "Generous"), ("metric", "pledges_delivered"), ("threshold", 1), ("sort_order", 65),
    ("criteria", "1 pledge delivered"),
])
def test_the_donor_badge_is_seeded(field, expected):
    assert getattr(Badge.objects.get(pk="first_pledge"), field) == expected


@pytest.mark.django_db
def test_the_donor_badge_sits_between_the_rehome_badges_and_bayani():
    codes = list(Badge.objects.order_by("sort_order", "badge_code").values_list("pk", flat=True))
    assert codes.index("rehomed_5") < codes.index("first_pledge") < codes.index("bayani")


# ── the event: a shelter confirms receipt ───────────────────────
@pytest.mark.django_db
def test_a_confirmed_pledge_earns_the_donor_the_badge(client, django_capture_on_commit_callbacks):
    shelter, donor = _shelter(), AccountFactory()
    need = _need(shelter)
    pledge = _pledge(need, donor)
    with django_capture_on_commit_callbacks(execute=True):
        assert _confirm(client, shelter, need, pledge).status_code == 200
    assert _held(donor) == {"first_pledge"}
    assert Notification.objects.filter(account=donor, type="badge_earned").count() == 1


@pytest.mark.django_db
def test_the_shelter_that_confirmed_earns_nothing(client, django_capture_on_commit_callbacks):
    shelter, donor = _shelter(), AccountFactory()
    need = _need(shelter)
    with django_capture_on_commit_callbacks(execute=True):
        _confirm(client, shelter, need, _pledge(need, donor))
    assert _held(shelter) == set()


@pytest.mark.django_db
def test_the_event_says_it_came_from_a_pledge(client, django_capture_on_commit_callbacks):
    shelter, donor = _shelter(), AccountFactory()
    need = _need(shelter)
    with mock.patch("community.badges.emit") as emit, django_capture_on_commit_callbacks(execute=True):
        _confirm(client, shelter, need, _pledge(need, donor))
    emit.assert_called_once_with("badge_earned", badge_code="first_pledge", source="pledge")


@pytest.mark.django_db
def test_a_second_delivered_pledge_earns_nothing_new(client, django_capture_on_commit_callbacks):
    shelter, donor = _shelter(), AccountFactory()
    need = _need(shelter, needed=10)
    with django_capture_on_commit_callbacks(execute=True):
        _confirm(client, shelter, need, _pledge(need, donor))
        _confirm(client, shelter, need, _pledge(need, donor))
    assert _held(donor) == {"first_pledge"}
    assert Notification.objects.filter(account=donor, type="badge_earned").count() == 1


@pytest.mark.django_db
def test_a_pledge_that_is_only_made_earns_nothing(django_capture_on_commit_callbacks):
    donor = AccountFactory()
    _pledge(_need(), donor)
    with django_capture_on_commit_callbacks(execute=True):
        award_badges_for(donor)
    assert _held(donor) == set()


@pytest.mark.django_db
def test_a_cancelled_pledge_earns_nothing():
    donor = AccountFactory()
    _pledge(_need(), donor, status=PledgeStatus.CANCELLED)
    assert award_badges_for(donor) == []


@pytest.mark.django_db
def test_a_confirm_that_is_refused_awards_nothing(client, django_capture_on_commit_callbacks):
    """A closed need refuses a late confirmation (need_not_open): the pledge stays pledged, so there is no badge."""
    from community.models import NeedStatus
    shelter, donor = _shelter(), AccountFactory()
    need = _need(shelter, status=NeedStatus.CLOSED)
    with django_capture_on_commit_callbacks(execute=True):
        res = _confirm(client, shelter, need, _pledge(need, donor))
    assert res.status_code >= 400 and _held(donor) == set()


@pytest.mark.django_db
def test_a_failing_award_does_not_fail_the_confirmation(client, django_capture_on_commit_callbacks):
    """IB2 · a badge never gets a vote on the work that earned it."""
    shelter, donor = _shelter(), AccountFactory()
    need = _need(shelter)
    pledge = _pledge(need, donor)
    with mock.patch("community.badges.award_badges_for", side_effect=RuntimeError("boom")), \
            django_capture_on_commit_callbacks(execute=True):
        res = _confirm(client, shelter, need, pledge)
    pledge.refresh_from_db()
    assert res.status_code == 200 and pledge.status == PledgeStatus.DELIVERED


# ── the sweep ───────────────────────────────────────────────────
@pytest.mark.django_db
def test_a_delivered_pledge_makes_its_donor_a_sweep_candidate():
    """IB20 dropped pledge-only accounts because no badge used pledges. One does now."""
    donor = AccountFactory()
    _delivered(donor)
    assert donor.pk in _candidate_account_ids()


@pytest.mark.django_db
def test_a_pledge_that_is_not_delivered_is_not_a_candidate():
    donor = AccountFactory()
    _pledge(_need(), donor)
    assert donor.pk not in _candidate_account_ids()


@pytest.mark.django_db
def test_the_sweep_badges_a_donor_whose_hook_missed():
    donor = AccountFactory()
    _delivered(donor)                                    # written directly: no event hook ran
    touched, failed = award_badges()
    assert failed == [] and (donor.pk, ["first_pledge"]) in touched
    assert _held(donor) == {"first_pledge"}


@pytest.mark.django_db
def test_the_sweep_does_not_badge_a_suspended_donor():
    donor = AccountFactory()
    _delivered(donor)
    Account.objects.filter(pk=donor.pk).update(status=AccountStatus.SUSPENDED)
    award_badges()
    assert _held(donor) == set()


# ── what the impact screen reads ────────────────────────────────
@pytest.mark.django_db
def test_the_impact_read_shows_the_donor_badge_earned(client):
    donor = AccountFactory()
    _delivered(donor)
    award_badges_for(donor)
    body = client.get("/api/v1/me/impact", **_hdr(donor)).json()
    badge = {b["badge_code"]: b for b in body["badges"]}["first_pledge"]
    assert badge["earned"] is True and badge["progress"] == {"current": 1, "target": 1}
    assert body["impact"]["pledges_delivered"] == 1


@pytest.mark.django_db
def test_a_non_donor_sees_it_at_zero_of_one(client):
    body = client.get("/api/v1/me/impact", **_hdr(AccountFactory())).json()
    badge = {b["badge_code"]: b for b in body["badges"]}["first_pledge"]
    assert badge["earned"] is False and badge["progress"] == {"current": 0, "target": 1}


@pytest.mark.django_db
def test_giving_is_not_one_of_the_three_areas_bayani_needs(client):
    """Bayani is shift + rescue + rehome. A donor with nothing else is 0 of 3, not 1."""
    donor = AccountFactory()
    _delivered(donor)
    body = client.get("/api/v1/me/impact", **_hdr(donor)).json()
    assert {b["badge_code"]: b for b in body["badges"]}["bayani"]["progress"] == {"current": 0, "target": 3}
