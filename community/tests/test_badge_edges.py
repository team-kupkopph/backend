"""Impact & badges test plan, Run 2 (NT-1 to NT-7): the rows Run 1 could not point a test at.

Each test names the plan row it holds (dev/test-plan-impact-badges.md in the library repo). They pin behaviour that already
shipped, so they pass on first run when the behaviour is right, and a failure is a finding, not a typo.
"""
import logging
import threading
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import OperationalError, connection
from django.test import Client
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.factories import AccountFactory
from accounts.lifecycle import soft_delete_account
from accounts.models import AccountStatus
from accounts.purge import anonymize_expired_accounts
from accounts.tokens import tokens_for
from community.badges import award_badges_for, earned_codes
from community.models import AccountBadge, Badge
from community.sweeps import award_badges
from community.tests.test_badge_evidence import _case
from community.tests.test_badge_sweep import _volunteer
from community.tests.test_badges_safe_award import _signup
from community.tests.test_badges_undo import _held, _notices, _undo, _volunteer_with
from community.tests.test_needs import _hdr
from community.tests.test_needs_integrity import _race
from listings.tests.adoption_helpers import c
from volunteer.models import SignupStatus

ZERO = {"shifts_completed": 0, "rescues_helped": 0, "pets_rehomed": 0, "pledges_delivered": 0}


@pytest.fixture
def first_shift_badge(db):
    """A transactional test flushes the database when it ends, which wipes the catalog the migrations seeded, and the next
    transactional test would find none (pytest-django runs them last, so only they are affected). Put back what these need."""
    Badge.objects.update_or_create(badge_code="first_shift", defaults={
        "name": "First Kawang-Gawa", "description": "Completed your first volunteer shift", "icon": "ti-paw",
        "criteria": "1 completed volunteer shift", "metric": "shifts_completed", "threshold": 1, "sort_order": 10})


class Killed(BaseException):
    """A process dying: not an `Exception`, so no `except Exception` in the code under test may swallow it."""


# ── NT-2 · U1: every threshold, one below, at, and one above, read from the catalog itself ──────────────────────
@pytest.mark.django_db
def test_u1_every_threshold_flips_exactly_at_its_number():
    catalog = list(Badge.objects.all())
    checked = 0
    for badge in catalog:
        if not badge.metric or not badge.threshold:
            continue
        for n in (badge.threshold - 1, badge.threshold, badge.threshold + 1):
            counts = dict(ZERO)
            if badge.metric == "areas_helped":
                # Bayani: the number is how many of the three areas are above zero.
                for area in ("shifts_completed", "rescues_helped", "pets_rehomed")[:n]:
                    counts[area] = 1
            else:
                counts[badge.metric] = n
            assert (badge.pk in earned_codes(counts, catalog)) == (n >= badge.threshold), (badge.pk, n)
            checked += 1
    assert checked >= 3 * 8        # the scan found the catalog: seven earlier badges and the donor one


# ── NT-2 · U4: a catalog that is empty, or short one badge ─────────────────────────────────────────────────────
@pytest.mark.django_db
def test_u4_an_empty_catalog_awards_nothing_and_raises_nothing():
    volunteer, _, _ = _volunteer_with(1)
    AccountBadge.objects.all().delete()
    Badge.objects.all().delete()
    assert award_badges_for(volunteer) == []


@pytest.mark.django_db
def test_u4_a_missing_row_costs_only_that_badge():
    volunteer = _volunteer(1)
    Badge.objects.filter(pk="first_shift").delete()
    assert award_badges_for(volunteer) == []
    # Another badge, one that is still seeded, is unaffected by the hole.
    assert Badge.objects.filter(pk="shifts_10").exists()


# ── NT-2 · U9: a case that expired is not a second rescue ──────────────────────────────────────────────────────
@pytest.mark.django_db
def test_u9_an_expired_case_that_was_also_resolved_counts_once():
    from community.badges import impact_counts
    claimer = AccountFactory()
    _, case = _case(claimer, notes="Reunited with the owner")
    case.expired_at = timezone.now()
    case.save(update_fields=["expired_at"])
    assert impact_counts(claimer)["rescues_helped"] == 1


@pytest.mark.django_db
def test_u9_an_expired_case_that_was_never_resolved_does_not_count():
    from community.badges import impact_counts
    claimer = AccountFactory()
    _, case = _case(claimer, notes="Reunited with the owner")
    case.resolved_at = None
    case.expired_at = timezone.now()
    case.save(update_fields=["resolved_at", "expired_at"])
    assert impact_counts(claimer)["rescues_helped"] == 0


# ── NT-1 · U6 / F3: two awards at once make one badge and one notice ───────────────────────────────────────────
@pytest.mark.django_db
def test_u6_a_concurrent_insert_is_absorbed_with_no_notice_event_or_error(caplog):
    volunteer = _volunteer(1)
    real = AccountBadge.objects.get_or_create

    def lose_the_race(**kwargs):
        AccountBadge.objects.create(account=kwargs["account"], badge_id=kwargs["badge_id"])   # the other writer got there first
        return real(**kwargs)

    with mock.patch.object(AccountBadge.objects, "get_or_create", side_effect=lose_the_race), \
            mock.patch("community.badges.emit") as emit, caplog.at_level(logging.ERROR, logger="kupkop.badges"):
        awarded = award_badges_for(volunteer)
    assert awarded == []
    assert _held(volunteer) == {"first_shift"}
    assert _notices(volunteer).count() == 0
    emit.assert_not_called()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.django_db(transaction=True)
def test_f3_two_awards_at_once_make_one_badge_and_one_notice(first_shift_badge):
    volunteer = _volunteer(1)

    class Result:
        status_code = 200

    def award():
        award_badges_for(volunteer)
        return Result()

    assert _race(2, award) == [200, 200]
    assert _held(volunteer) == {"first_shift"}
    assert _notices(volunteer).count() == 1


# ── NT-1 · F4: attendance marked twice at once ─────────────────────────────────────────────────────────────────
@pytest.mark.django_db(transaction=True)
def test_f4_attendance_marked_twice_at_once_is_one_200_one_409_and_one_badge(first_shift_badge):
    volunteer, shelter_account = AccountFactory(), AccountFactory(account_type="shelter")
    _, signup = _signup(volunteer)
    signup.shift.shelter_account = shelter_account
    signup.shift.save(update_fields=["shelter_account"])
    codes = _race(2, lambda: Client().post(f"/api/v1/shelter/signups/{signup.pk}/attendance",
                                           {"outcome": "completed"}, content_type="application/json", **_hdr(shelter_account)))
    assert sorted(codes) == [200, 409]
    assert _held(volunteer) == {"first_shift"}
    assert _notices(volunteer).count() == 1


# ── NT-1 · F5: attendance racing the volunteer's cancel ────────────────────────────────────────────────────────
@pytest.mark.django_db(transaction=True)
def test_f5_whichever_wins_a_badge_exists_only_if_the_shift_was_completed(first_shift_badge):
    volunteer, shelter_account = AccountFactory(), AccountFactory(account_type="shelter")
    _, signup = _signup(volunteer)
    signup.shift.shelter_account = shelter_account
    signup.shift.save(update_fields=["shelter_account"])
    barrier = threading.Barrier(2)
    out = []

    def mark():
        barrier.wait()
        try:
            out.append(Client().post(f"/api/v1/shelter/signups/{signup.pk}/attendance", {"outcome": "completed"},
                                     content_type="application/json", **_hdr(shelter_account)).status_code)
        finally:
            connection.close()

    def cancel():
        barrier.wait()
        try:
            out.append(Client().post(f"/api/v1/signups/{signup.pk}/cancel", {}, content_type="application/json",
                                     **_hdr(volunteer)).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=mark), threading.Thread(target=cancel)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    signup.refresh_from_db()
    assert all(code < 500 for code in out), out
    assert (_held(volunteer) == {"first_shift"}) == (signup.status == SignupStatus.COMPLETED)


# ── NT-4 · I3: the connection drops after the business commit, before the award ────────────────────────────────
@pytest.mark.django_db
def test_i3_a_dropped_connection_at_award_time_leaves_the_work_and_the_sweep_repairs_it(django_capture_on_commit_callbacks):
    volunteer, shelter_account = AccountFactory(), AccountFactory(account_type="shelter")
    _, signup = _signup(volunteer)
    signup.shift.shelter_account = shelter_account
    signup.shift.save(update_fields=["shelter_account"])
    drop = OperationalError("server closed the connection unexpectedly")
    with mock.patch("community.badges.award_badges_for", side_effect=drop), django_capture_on_commit_callbacks(execute=True):
        res = c(shelter_account).post(f"/api/v1/shelter/signups/{signup.pk}/attendance", {"outcome": "completed"}, format="json")
    signup.refresh_from_db()
    assert res.status_code == 200 and signup.status == SignupStatus.COMPLETED
    assert _held(volunteer) == set()
    touched, failed = award_badges()                    # the next night's run
    assert failed == [] and _held(volunteer) == {"first_shift"}


# ── NT-4 · I4: a statement timeout during /me/impact ───────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_i4_a_statement_timeout_on_my_impact_is_a_json_500_with_no_internals(caplog):
    account = AccountFactory()
    seen = {"n": 0}

    def time_out_second_query(execute, sql, params, many, context):
        seen["n"] += 1
        if seen["n"] == 2:
            raise OperationalError("canceling statement due to statement timeout")
        return execute(sql, params, many, context)

    with caplog.at_level(logging.ERROR, logger="django.request"), connection.execute_wrapper(time_out_second_query):
        res = Client(raise_request_exception=False).get("/api/v1/me/impact", **_hdr(account))
    assert res.status_code == 500
    assert res["Content-Type"].startswith("application/json"), res.content[:120]
    assert res.json()["error"]["code"] == "server_error"
    assert "statement timeout" not in res.content.decode()


# ── NT-4 · I6: every token the API cannot honour is a 401 on /me/impact, never a 500 ───────────────────────────
def _bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _expired_token(account):
    token = RefreshToken()
    token["account_id"] = str(account.account_id)
    access = token.access_token
    access.set_exp(from_time=timezone.now() - timezone.timedelta(hours=2), lifetime=timezone.timedelta(minutes=1))
    return str(access)


@pytest.mark.django_db
@pytest.mark.parametrize("case", ["expired", "revoked", "suspended", "deleted", "staff", "garbage"])
def test_i6_a_token_that_cannot_be_honoured_is_a_401(case):
    account = AccountFactory()
    token = tokens_for(account)["access"]
    if case == "expired":
        token = _expired_token(account)
    elif case == "revoked":
        account.sessions_revoked_at = timezone.now() + timezone.timedelta(minutes=1)
        account.save(update_fields=["sessions_revoked_at"])
    elif case == "suspended":
        account.status = AccountStatus.SUSPENDED
        account.save(update_fields=["status"])
    elif case == "deleted":
        soft_delete_account(account)
    elif case == "staff":
        staff = RefreshToken()
        staff["staff_user_id"] = 1
        token = str(staff.access_token)
    elif case == "garbage":
        token = "not-a-token"
    res = Client(raise_request_exception=False).get("/api/v1/me/impact", **_bearer(token))
    assert res.status_code == 401, (case, res.status_code)


# ── NT-5 · I11: the 24 h undo window, at both edges ─────────────────────────────────────────────────────────────
@pytest.mark.django_db
@pytest.mark.parametrize("marked_ago, code, kept", [
    (timezone.timedelta(hours=23, minutes=59), 200, set()),
    (timezone.timedelta(hours=24, minutes=1), 409, {"first_shift"}),
])
def test_i11_the_undo_window_closes_at_24_hours(marked_ago, code, kept):
    volunteer, shelter_account, signup = _volunteer_with(1)
    signup.attendance_marked_at = timezone.now() - marked_ago
    signup.save(update_fields=["attendance_marked_at"])
    res = _undo(shelter_account, signup)
    assert res.status_code == code
    if code == 409:
        assert res.json()["error"]["code"] == "undo_expired"
    assert _held(volunteer) == kept


# ── NT-6 · I10: export lists the badge; a purged holder keeps it and earns no more ──────────────────────────────
@pytest.mark.django_db
def test_i10_the_export_lists_a_held_badge():
    volunteer, _, _ = _volunteer_with(1)
    body = Client().get("/api/v1/me/export", **_hdr(volunteer)).json()
    assert [b.get("badge_code") or b.get("code") for b in body["badges"]] == ["first_shift"]


@pytest.mark.django_db
def test_i10_a_purged_holder_keeps_the_welfare_record_and_the_sweep_leaves_them_alone():
    volunteer, _, _ = _volunteer_with(1)
    soft_delete_account(volunteer)
    volunteer.deleted_at = timezone.now() - timezone.timedelta(days=400)
    volunteer.save(update_fields=["deleted_at"])
    assert anonymize_expired_accounts() != []
    assert _held(volunteer) == {"first_shift"}           # §12.7: the record of what was done stays
    for _ in range(9):                                    # a shift that completes later would otherwise earn a second badge
        _signup(volunteer, SignupStatus.COMPLETED)
    before = _notices(volunteer).count()
    touched, failed = award_badges()
    assert touched == [] and failed == []
    assert _held(volunteer) == {"first_shift"} and _notices(volunteer).count() == before


# ── NT-7 · F7: the sweep killed part-way, then run again ────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_f7_a_killed_sweep_keeps_what_it_finished_and_the_next_run_does_the_rest():
    accounts = [_volunteer(1) for _ in range(3)]
    real = award_badges_for
    calls = {"n": 0}

    def die_on_second(account, source="sweep"):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Killed()
        return real(account, source=source)

    with mock.patch("community.sweeps.award_badges_for", side_effect=die_on_second), pytest.raises(Killed):
        call_command("run_badge_sweep")
    done = [a for a in accounts if _held(a)]
    assert len(done) == 1 and _notices(done[0]).count() == 1
    call_command("run_badge_sweep")                       # also proves the singleton lock was released by the death
    assert all(_held(a) == {"first_shift"} for a in accounts)
    assert all(_notices(a).count() == 1 for a in accounts)


# ── NT-7 · F8: a deadlock or lock wait loses this account for tonight only ──────────────────────────────────────
@pytest.mark.django_db
def test_f8_a_deadlock_on_one_account_is_logged_skipped_and_retried_next_run(caplog):
    unlucky, fine = _volunteer(1), _volunteer(1)
    real = award_badges_for

    def deadlock_for_unlucky(account, source="sweep"):
        if account.pk == unlucky.pk:
            raise OperationalError("deadlock detected")
        return real(account, source=source)

    with mock.patch("community.sweeps.award_badges_for", side_effect=deadlock_for_unlucky), \
            caplog.at_level(logging.ERROR, logger="kupkop.badges"):
        touched, failed = award_badges()
    assert failed == [unlucky.pk] and _held(fine) == {"first_shift"} and _held(unlucky) == set()
    assert any("badge sweep failed" in r.getMessage() for r in caplog.records)
    touched, failed = award_badges()
    assert failed == [] and _held(unlucky) == {"first_shift"}


# ── NT-6 · F14 (fixed): an account deleted between the sweep's read and its award ───────────────────────────────────────
@pytest.mark.django_db
def test_f14_an_account_deleted_after_the_sweep_read_it_is_not_badged_or_told():
    volunteer = _volunteer(1)
    real = award_badges_for

    def deleted_just_before(account, source="sweep"):
        soft_delete_account(account)
        return real(account, source=source)

    with mock.patch("community.sweeps.award_badges_for", side_effect=deleted_just_before):
        award_badges()
    assert _held(volunteer) == set() and _notices(volunteer).count() == 0
