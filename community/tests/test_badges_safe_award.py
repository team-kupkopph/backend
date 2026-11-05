"""IB2 / IB3 / IB18 · a badge is a celebration, so it never gets a vote on the work that earned it.

A failing award used to roll back an adoption, or answer 500 for attendance and a case status
that had already saved (and the shelter's retry then got a 409). `award_after_commit` runs after
the writer's transaction and swallows its own failures: the sweep repairs a missed badge.
"""
from unittest import mock

import pytest
from django.contrib.gis.geos import Point
from django.db import transaction
from django.utils import timezone

from accounts.factories import AccountFactory
from community.badges import award_after_commit, award_badges_for
from community.models import AccountBadge
from listings.models import AdoptionListing, ListingStatus
from listings.tests.adoption_helpers import c, inquire, listing, person, post, shelter
from notifications.models import Notification
from sagip.models import RescueCase, StrayReport, StrayStatus
from verifications.models import AccountCapability
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.status import set_signup_status

BOOM = RuntimeError("boom")


def _client(account):
    client = c(account)
    client.raise_request_exception = False      # a 500 is the failure under test, not a crash
    return client


def _signup(volunteer, status=SignupStatus.APPROVED):
    shelter_account = AccountFactory(account_type="shelter")
    now = timezone.now()
    shift = VolunteerShift.objects.create(shelter_account=shelter_account,
                                          starts_at=now - timezone.timedelta(hours=3),
                                          ends_at=now - timezone.timedelta(hours=1))
    return shelter_account, VolunteerSignup.objects.create(shift=shift, volunteer_account=volunteer,
                                                           status=status, waiver_accepted=True)


def _claimed_case(claimer):
    report = StrayReport.objects.create(reporter_account=AccountFactory(), species="dog",
                                        condition="injured", status=StrayStatus.CLAIMED,
                                        geom=Point(121.05, 14.63, srid=4326))
    return RescueCase.objects.create(report=report, claimed_by_account=claimer)


def _completed_shift(account):
    _signup(account, SignupStatus.COMPLETED)


# ── the five writers ────────────────────────────────────────────
@pytest.mark.django_db
def test_a_failing_award_leaves_attendance_recorded(django_capture_on_commit_callbacks):
    shelter_account, signup = _signup(AccountFactory())
    with mock.patch("community.badges.award_badges_for", side_effect=BOOM), \
            django_capture_on_commit_callbacks(execute=True):
        res = _client(shelter_account).post(f"/api/v1/shelter/signups/{signup.pk}/attendance",
                                            {"outcome": "completed"}, format="json")
    signup.refresh_from_db()
    assert res.status_code == 200 and signup.status == SignupStatus.COMPLETED


@pytest.mark.django_db
def test_a_failing_award_still_counts_the_completed_shift(django_capture_on_commit_callbacks):
    """`emit` follows the award in the view, so a raise used to lose the event as well."""
    shelter_account, signup = _signup(AccountFactory())
    with mock.patch("community.badges.award_badges_for", side_effect=BOOM), \
            mock.patch("common.analytics.emit") as emit, \
            django_capture_on_commit_callbacks(execute=True):
        _client(shelter_account).post(f"/api/v1/shelter/signups/{signup.pk}/attendance",
                                      {"outcome": "completed"}, format="json")
    assert mock.call("shift_completed") in emit.call_args_list


@pytest.mark.django_db
def test_a_failing_award_leaves_the_case_resolved(django_capture_on_commit_callbacks):
    claimer = AccountFactory()
    case = _claimed_case(claimer)
    with mock.patch("community.badges.award_badges_for", side_effect=BOOM), \
            django_capture_on_commit_callbacks(execute=True):
        res = _client(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": "resolved"},
                                    format="json")
    case.refresh_from_db()
    assert res.status_code == 200 and case.resolved_at is not None


@pytest.mark.django_db
def test_a_failing_award_does_not_undo_mark_adopted(django_capture_on_commit_callbacks):
    poster = person(member=True)
    the_listing = listing(poster, status=ListingStatus.AVAILABLE)
    with mock.patch("community.badges.award_badges_for", side_effect=BOOM), \
            django_capture_on_commit_callbacks(execute=True):
        res = _client(poster).post(f"/api/v1/listings/{the_listing.pk}/mark-adopted")
    the_listing.refresh_from_db()
    assert res.status_code == 200 and the_listing.status == ListingStatus.ADOPTED


@pytest.mark.django_db
def test_a_failing_award_does_not_undo_a_completed_adoption(django_capture_on_commit_callbacks):
    poster = shelter()
    the_listing = listing(poster)
    inquiry = inquire(person(member=True), the_listing)
    post(poster, inquiry, "screen")
    post(poster, inquiry, "reserve")
    with mock.patch("community.badges.award_badges_for", side_effect=BOOM), \
            django_capture_on_commit_callbacks(execute=True):
        client = _client(poster)
        res = client.post(f"/api/v1/inquiries/{inquiry.pk}/complete", format="json")
    the_listing.refresh_from_db()
    assert res.status_code == 200 and the_listing.status == ListingStatus.ADOPTED


@pytest.mark.django_db
def test_a_failing_award_does_not_undo_an_accepted_placement(django_capture_on_commit_callbacks):
    rescuer = AccountFactory()
    recipient = AccountFactory()
    AccountCapability.objects.create(account=recipient, capability="rescuer", status="approved")
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
                                        status=StrayStatus.SAFE, geom=Point(121.05, 14.63, srid=4326))
    case = RescueCase.objects.create(report=report, claimed_by_account=rescuer)
    inquiry_id = c(rescuer).post(f"/api/v1/cases/{case.pk}/place",
                                 {"recipient_email": recipient.email, "name": "Bruno",
                                  "adoption_fee": "0"}, format="json").json()["inquiry_id"]
    with mock.patch("community.badges.award_badges_for", side_effect=BOOM), \
            django_capture_on_commit_callbacks(execute=True):
        res = _client(recipient).post(f"/api/v1/inquiries/{inquiry_id}/accept")
    assert res.status_code == 200
    assert AdoptionListing.objects.get(source_report=report).status == ListingStatus.ADOPTED


# ── the hook still awards ───────────────────────────────────────
@pytest.mark.django_db
def test_recording_a_completed_shift_awards_from_the_status_writer(django_capture_on_commit_callbacks):
    """D-S6-3 names `set_signup_status(completed)` as the writer, so no caller can skip it."""
    volunteer = AccountFactory()
    _, signup = _signup(volunteer)
    with django_capture_on_commit_callbacks(execute=True):
        set_signup_status(signup, SignupStatus.COMPLETED)
    assert AccountBadge.objects.filter(account=volunteer, badge_id="first_shift").exists()


@pytest.mark.django_db
def test_a_no_show_awards_nothing(django_capture_on_commit_callbacks):
    volunteer = AccountFactory()
    _, signup = _signup(volunteer)
    with django_capture_on_commit_callbacks(execute=True):
        set_signup_status(signup, SignupStatus.NO_SHOW)
    assert not AccountBadge.objects.filter(account=volunteer).exists()


@pytest.mark.django_db
def test_completing_an_adoption_still_awards_the_poster(django_capture_on_commit_callbacks):
    poster = shelter()
    inquiry = inquire(person(member=True), listing(poster))
    post(poster, inquiry, "screen")
    post(poster, inquiry, "reserve")
    with django_capture_on_commit_callbacks(execute=True):
        assert post(poster, inquiry, "complete").status_code == 200
    assert AccountBadge.objects.filter(account=poster, badge_id="rehomed_1").exists()


@pytest.mark.django_db
def test_resolving_a_case_still_awards_the_claimer(django_capture_on_commit_callbacks):
    claimer = AccountFactory()
    case = _claimed_case(claimer)
    with django_capture_on_commit_callbacks(execute=True):
        c(claimer).post(f"/api/v1/cases/{case.pk}/status",
                        {"status": "resolved", "outcome_notes": "Home safe"}, format="json")
    assert AccountBadge.objects.filter(account=claimer, badge_id="first_rescue").exists()


# ── award_after_commit ──────────────────────────────────────────
@pytest.mark.django_db
def test_nothing_is_awarded_when_the_transaction_rolls_back(django_capture_on_commit_callbacks):
    account = AccountFactory()
    _completed_shift(account)
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                award_after_commit(account, source="shift")
                raise BOOM
    assert callbacks == [] and not AccountBadge.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_a_failed_award_is_logged(django_capture_on_commit_callbacks, caplog):
    account = AccountFactory()
    with mock.patch("community.badges.award_badges_for", side_effect=BOOM), \
            django_capture_on_commit_callbacks(execute=True):
        award_after_commit(account, source="shift")
    assert any(r.name == "kupkop.badges" and r.exc_info for r in caplog.records)


@pytest.mark.django_db
def test_the_event_names_where_the_award_came_from(django_capture_on_commit_callbacks):
    account = AccountFactory()
    _completed_shift(account)
    with mock.patch("community.badges.emit") as emit, \
            django_capture_on_commit_callbacks(execute=True):
        award_after_commit(account, source="shift")
    emit.assert_called_once_with("badge_earned", badge_code="first_shift", source="shift")


@pytest.mark.django_db
def test_a_sweep_award_says_so():
    account = AccountFactory()
    _completed_shift(account)
    from community.sweeps import award_badges
    with mock.patch("community.badges.emit") as emit:
        award_badges()
    emit.assert_called_once_with("badge_earned", badge_code="first_shift", source="sweep")


# ── IB3 · a badge and its notice land together ──────────────────
@pytest.mark.django_db
def test_a_failed_notice_leaves_the_badge_for_the_next_run():
    """The badge used to survive without its notice, and `have` then hid it from every rerun."""
    account = AccountFactory()
    _completed_shift(account)
    with mock.patch("community.badges.notify", side_effect=BOOM):
        assert award_badges_for(account) == []
    assert not AccountBadge.objects.filter(account=account).exists()
    assert award_badges_for(account) == ["first_shift"]
    assert Notification.objects.filter(account=account, type="badge_earned").count() == 1


@pytest.mark.django_db
def test_one_failed_notice_does_not_stop_the_other_badges():
    account = AccountFactory()
    for _ in range(10):
        _completed_shift(account)
    from community.badges import notify as real_notify

    def fail_first(acc, type, **kw):
        if kw["data"]["badge_code"] == "first_shift":
            raise BOOM
        return real_notify(acc, type, **kw)

    with mock.patch("community.badges.notify", side_effect=fail_first):
        assert award_badges_for(account) == ["shifts_10"]
    assert set(AccountBadge.objects.filter(account=account).values_list("badge_id", flat=True)) \
        == {"shifts_10"}
