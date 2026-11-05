"""IB17 / IB20 / IB23 / IB24 · the badge sweep is a nightly job, batched, and one bad account
never stops the rest.

It sat in the hourly `run_sweeps` under a comment saying a sweep joins that list only when a
one-hour delay would hurt someone (a late badge hurts nobody), cost 9 queries per account on an
idle run, badged accounts that could not sign in, and let one raising account abort the rest.
"""
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Account, AccountStatus
from community.badges import award_badges_for, impact_counts_many
from community.models import AccountBadge, NeedPledge, PledgeStatus, ShelterNeed
from community.sweeps import _candidate_account_ids, award_badges
from notifications.models import Notification
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup


def _shift_for(volunteer, status=SignupStatus.COMPLETED):
    shelter = AccountFactory(account_type="shelter")
    now = timezone.now()
    shift = VolunteerShift.objects.create(shelter_account=shelter,
                                          starts_at=now - timezone.timedelta(hours=3),
                                          ends_at=now - timezone.timedelta(hours=1))
    return VolunteerSignup.objects.create(shift=shift, volunteer_account=volunteer, status=status,
                                          waiver_accepted=True)


def _volunteer(shifts=1):
    account = AccountFactory()
    for _ in range(shifts):
        _shift_for(account)
    return account


def _held(account):
    return set(AccountBadge.objects.filter(account=account).values_list("badge_id", flat=True))


# ── impact_counts_many ──────────────────────────────────────────
@pytest.mark.django_db
def test_counts_for_no_accounts_is_empty():
    assert impact_counts_many([]) == {}


@pytest.mark.django_db
def test_an_account_with_no_activity_reads_zero_not_missing():
    nobody = AccountFactory()
    assert impact_counts_many([nobody]) == {nobody.pk: {
        "shifts_completed": 0, "rescues_helped": 0, "pets_rehomed": 0, "pledges_delivered": 0}}


@pytest.mark.django_db
def test_counts_are_per_account():
    two, five = _volunteer(2), _volunteer(5)
    counts = impact_counts_many([two, five])
    assert counts[two.pk]["shifts_completed"] == 2 and counts[five.pk]["shifts_completed"] == 5


@pytest.mark.django_db
def test_pledges_delivered_are_counted_for_the_pledger():
    donor = AccountFactory()
    need = ShelterNeed.objects.create(shelter_account=AccountFactory(account_type="shelter"),
                                      title="Dog food", category="food", quantity_needed=10)
    NeedPledge.objects.create(need=need, pledger_account=donor, quantity=2,
                              status=PledgeStatus.DELIVERED)
    NeedPledge.objects.create(need=need, pledger_account=donor, quantity=1,
                              status=PledgeStatus.PLEDGED)
    assert impact_counts_many([donor])[donor.pk]["pledges_delivered"] == 1


@pytest.mark.django_db
def test_the_single_account_read_is_the_batched_one():
    """US-B1: do not fork a second counting path for the same facts."""
    from community.badges import impact_counts
    account = _volunteer(3)
    assert impact_counts(account) == impact_counts_many([account])[account.pk]


@pytest.mark.django_db
def test_the_batched_counts_cost_the_same_for_one_account_or_many():
    def cost(accounts):
        with CaptureQueriesContext(connection) as ctx:
            impact_counts_many(accounts)
        return len(ctx)

    one = cost([_volunteer(1)])
    many = cost([_volunteer(1) for _ in range(8)])
    assert one == many


# ── who the sweep looks at ──────────────────────────────────────
@pytest.mark.django_db
def test_a_deleted_account_is_not_badged_or_told():
    gone = _volunteer(1)
    Account.objects.filter(pk=gone.pk).update(status=AccountStatus.DELETED,
                                              deleted_at=timezone.now())
    award_badges()
    assert not AccountBadge.objects.filter(account=gone).exists()
    assert not Notification.objects.filter(account=gone, type="badge_earned").exists()


@pytest.mark.django_db
def test_a_suspended_account_is_not_badged_or_told():
    held = _volunteer(1)
    Account.objects.filter(pk=held.pk).update(status=AccountStatus.SUSPENDED)
    award_badges()
    assert not AccountBadge.objects.filter(account=held).exists()
    assert not Notification.objects.filter(account=held, type="badge_earned").exists()


@pytest.mark.django_db
def test_a_suspended_account_is_badged_when_it_comes_back():
    held = _volunteer(1)
    Account.objects.filter(pk=held.pk).update(status=AccountStatus.SUSPENDED)
    award_badges()
    Account.objects.filter(pk=held.pk).update(status=AccountStatus.ACTIVE)
    award_badges()
    assert _held(held) == {"first_shift"}


@pytest.mark.django_db
def test_an_active_account_is_still_badged():
    volunteer = _volunteer(1)
    touched, failed = award_badges()
    assert [(pk, codes) for pk, codes in touched if pk == volunteer.pk] \
        == [(volunteer.pk, ["first_shift"])]
    assert failed == []


@pytest.mark.django_db
def test_a_pledge_only_account_is_not_a_candidate():
    """No badge uses pledges, so checking such an account every night finds nothing."""
    donor = AccountFactory()
    need = ShelterNeed.objects.create(shelter_account=AccountFactory(account_type="shelter"),
                                      title="Dog food", category="food", quantity_needed=10)
    NeedPledge.objects.create(need=need, pledger_account=donor, quantity=2,
                              status=PledgeStatus.DELIVERED)
    assert donor.pk not in _candidate_account_ids()


# ── cost ────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_an_idle_sweep_costs_the_same_for_three_accounts_or_eight():
    """IB23 · 9 queries per account when nothing had changed."""
    def idle_cost():
        award_badges()                      # everyone now holds what they qualify for
        with CaptureQueriesContext(connection) as ctx:
            award_badges()
        return len(ctx)

    for _ in range(3):
        _volunteer(1)
    three = idle_cost()
    for _ in range(5):
        _volunteer(1)
    assert idle_cost() == three


@pytest.mark.django_db
def test_an_idle_sweep_writes_nothing():
    _volunteer(1)
    award_badges()
    with CaptureQueriesContext(connection) as ctx:
        touched, failed = award_badges()
    assert touched == [] and failed == []
    assert not [q for q in ctx.captured_queries
                if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]


@pytest.mark.django_db
def test_the_sweep_works_in_batches(monkeypatch):
    monkeypatch.setattr("community.sweeps.BATCH", 2)
    volunteers = [_volunteer(1) for _ in range(5)]
    award_badges()
    assert all(_held(v) == {"first_shift"} for v in volunteers)


# ── one bad account ─────────────────────────────────────────────
@pytest.mark.django_db
def test_one_failing_account_does_not_stop_the_rest():
    """IB24 · the second of five raised, and the sweep stopped with one account badged."""
    volunteers = sorted((_volunteer(1) for _ in range(5)), key=lambda a: str(a.pk))
    bad = volunteers[1]

    def flaky(account, source="sweep"):
        if account.pk == bad.pk:
            raise RuntimeError("boom")
        return award_badges_for(account, source=source)

    with mock.patch("community.sweeps.award_badges_for", side_effect=flaky):
        touched, failed = award_badges()
    assert failed == [bad.pk]
    assert {pk for pk, _ in touched} == {v.pk for v in volunteers} - {bad.pk}
    assert all(_held(v) == {"first_shift"} for v in volunteers if v.pk != bad.pk)


@pytest.mark.django_db
def test_a_failed_account_is_logged_and_retried_next_run(caplog):
    volunteer = _volunteer(1)
    with mock.patch("community.sweeps.award_badges_for", side_effect=RuntimeError("boom")):
        award_badges()
    assert any(r.name == "kupkop.badges" and r.exc_info for r in caplog.records)
    award_badges()
    assert _held(volunteer) == {"first_shift"}


# ── the command ─────────────────────────────────────────────────
@pytest.mark.django_db
def test_the_nightly_command_badges_and_reports():
    volunteer = _volunteer(1)
    out = StringIO()
    call_command("run_badge_sweep", stdout=out)
    assert _held(volunteer) == {"first_shift"}
    assert "badged 1" in out.getvalue() and "failed 0" in out.getvalue()


@pytest.mark.django_db
def test_a_partial_failure_still_exits_zero_and_says_so():
    _volunteer(1)
    _volunteer(1)
    calls = []

    def flaky(account, source="sweep"):
        calls.append(account.pk)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return award_badges_for(account, source=source)

    out = StringIO()
    with mock.patch("community.sweeps.award_badges_for", side_effect=flaky):
        call_command("run_badge_sweep", stdout=out)
    assert "badged 1" in out.getvalue() and "failed 1" in out.getvalue()


@pytest.mark.django_db
def test_a_sweep_where_every_account_failed_is_a_failed_run():
    _volunteer(1)
    with mock.patch("community.sweeps.award_badges_for", side_effect=RuntimeError("boom")):
        with pytest.raises(CommandError, match="failed 1"):
            call_command("run_badge_sweep", stdout=StringIO())


@pytest.mark.django_db
def test_the_run_is_counted_without_naming_anyone():
    _volunteer(1)
    with mock.patch("community.sweeps.emit") as emit:
        call_command("run_badge_sweep", stdout=StringIO())
    emit.assert_called_once_with("badge_sweep", accounts=1, awarded=1, failed=0)


@pytest.mark.django_db
def test_the_hourly_sweeps_no_longer_carry_badges():
    from sagip.management.commands.run_sweeps import SWEEPS
    assert "badged" not in [label for label, _ in SWEEPS]
