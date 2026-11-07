"""Review steps 12 and 13 (MO10, MO9, MQ6) · people who keep turning up, and the flags the system raises about them.

Held to: the count is derived (nothing stored but the flag); a rule raises one flag at a time and needs three MORE after one is decided;
it NEVER suspends or revokes; a rescue report is never resolved to a person; the withdrawal count follows §6.7 (the adopter's own
withdrawals, reset by an adoption, untouched by a poster declining or a takedown closing the listing)."""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import AccountStatus
from community.models import StoryPost
from listings.models import AdoptionInquiry, EndReason, InquiryStatus
from listings.tests.adoption_helpers import listing, person
from moderation import offenders
from moderation.actions import resolve_flag
from moderation.models import FlagStatus, ModerationFlag
from notifications.models import Notification
from sagip.models import StrayReport


def _flag(target_type, target_id, **kw):
    return ModerationFlag.objects.create(reporter_account=kw.pop("reporter", None) or AccountFactory(), target_type=target_type,
                                         target_id=target_id, reason="x", **kw)


def _take_down(poster, n=1):
    """n listings of `poster`, each taken down through the real decision."""
    done = []
    for _ in range(n):
        live = listing(poster)
        resolve_flag(_flag("listing", live.pk), AccountFactory(), FlagStatus.ACTIONED, reason_code="spam_scam")
        done.append(live)
    return done


# ---- step 12 · every flag about a person's own content ----------------------------------------------------------------------
@pytest.mark.django_db
def test_flags_about_a_person_cover_their_listings_stories_and_account_but_not_other_peoples():
    me, other = person(), person()
    mine_listing, theirs = listing(me), listing(other)
    story = StoryPost.objects.create(author_account=me, story_type="general", caption="x")
    ids = {_flag("listing", mine_listing.pk).pk, _flag("story", story.pk).pk, _flag("account", me.pk).pk}
    _flag("listing", theirs.pk)
    found = set(ModerationFlag.objects.filter(offenders.about_person(me)).values_list("pk", flat=True))
    assert found == ids


@pytest.mark.django_db
def test_a_rescue_report_is_never_resolved_to_its_reporter():
    """An anonymous report hides its reporter from everyone else; reading it back here would unmask them."""
    from django.contrib.gis.geos import Point
    reporter = person()
    report = StrayReport.objects.create(species="dog", condition="injured", geom=Point(121.05, 14.63, srid=4326), reporter_account=reporter)
    _flag("report", report.pk)
    assert not ModerationFlag.objects.filter(offenders.about_person(reporter)).exists()


@pytest.mark.django_db
def test_the_takedown_count_is_distinct_things_not_flags_and_only_the_last_90_days():
    poster = person()
    one = listing(poster)
    for _ in range(3):                                   # three reports about ONE listing, closed together
        _flag("listing", one.pk)
    resolve_flag(ModerationFlag.objects.filter(target_id=one.pk).first(), AccountFactory(), FlagStatus.ACTIONED, reason_code="spam_scam")
    assert offenders.takedown_count(poster) == 1
    old = listing(poster)
    flag = _flag("listing", old.pk, status=FlagStatus.ACTIONED, decision_reason_code="spam_scam")
    ModerationFlag.objects.filter(pk=flag.pk).update(reviewed_at=timezone.now() - timezone.timedelta(days=91))
    assert offenders.takedown_count(poster) == 1         # the 91-day-old one is outside the window


@pytest.mark.django_db
def test_a_dismissal_or_a_record_only_action_is_not_a_takedown():
    poster = person()
    resolve_flag(_flag("listing", listing(poster).pk), AccountFactory(), FlagStatus.DISMISSED)
    resolve_flag(_flag("account", poster.pk), AccountFactory(), FlagStatus.ACTIONED)
    assert offenders.takedown_count(poster) == 0


# ---- step 12 · three takedowns in 90 days raise a flag for a human (MQ6) -------------------------------------------------
@pytest.mark.django_db
def test_the_third_takedown_in_90_days_raises_a_system_flag_and_never_suspends():
    poster = person()
    _take_down(poster, 2)
    assert not ModerationFlag.objects.filter(rule=offenders.RULE_TAKEDOWN).exists()
    _take_down(poster, 1)
    system = ModerationFlag.objects.get(rule=offenders.RULE_TAKEDOWN)
    assert system.reporter_account_id is None and system.target_type == "account" and system.target_id == poster.pk
    assert system.status == FlagStatus.OPEN and "3 of this person's posts" in system.reason
    poster.refresh_from_db()
    assert poster.status == AccountStatus.ACTIVE                                  # a human decides; nothing is suspended


@pytest.mark.django_db
def test_a_fourth_takedown_does_not_raise_a_second_flag_while_the_first_is_open():
    poster = person()
    _take_down(poster, 4)
    assert ModerationFlag.objects.filter(rule=offenders.RULE_TAKEDOWN).count() == 1


@pytest.mark.django_db
def test_after_the_flag_is_decided_it_takes_three_more_to_raise_another():
    poster = person()
    _take_down(poster, 3)
    first = ModerationFlag.objects.get(rule=offenders.RULE_TAKEDOWN)
    resolve_flag(first, AccountFactory(), FlagStatus.DISMISSED)
    _take_down(poster, 2)
    assert ModerationFlag.objects.filter(rule=offenders.RULE_TAKEDOWN).count() == 1          # two more is not three
    _take_down(poster, 1)
    assert ModerationFlag.objects.filter(rule=offenders.RULE_TAKEDOWN).count() == 2


@pytest.mark.django_db
def test_the_system_flag_is_a_system_flag_in_the_queue_with_no_reporter_to_tell():
    poster = person()
    _take_down(poster, 3)
    system = ModerationFlag.objects.get(rule=offenders.RULE_TAKEDOWN)
    resolve_flag(system, AccountFactory(), FlagStatus.DISMISSED)                     # nobody to notify, and no crash
    assert not Notification.objects.filter(type="report_reviewed", data__flag_id=str(system.pk)).exists()


# ---- step 13 · the §6.7 repeat-withdrawal rule ---------------------------------------------------------------------------
def _inquiry(adopter, status=InquiryStatus.WITHDRAWN, end_reason=EndReason.ADOPTER_WITHDREW, minutes=0):
    row = AdoptionInquiry.objects.create(listing=listing(person()), adopter_account=adopter, status=status, end_reason=end_reason)
    AdoptionInquiry.objects.filter(pk=row.pk).update(decided_at=timezone.now() - timezone.timedelta(minutes=minutes))
    return row


@pytest.mark.django_db
def test_the_third_consecutive_withdrawal_raises_one_account_flag_for_review():
    adopter = person()
    for i in (30, 20):
        _inquiry(adopter, minutes=i)
    assert offenders.raise_repeat_withdrawal_flags() == []
    _inquiry(adopter, minutes=10)
    raised = offenders.raise_repeat_withdrawal_flags()
    assert len(raised) == 1
    flag = raised[0]
    assert flag.reporter_account_id is None and flag.target_type == "account" and flag.target_id == adopter.pk
    assert flag.rule == offenders.RULE_WITHDRAWAL and "3 adoption inquiries withdrawn in a row" in flag.reason
    adopter.refresh_from_db()
    assert adopter.status == AccountStatus.ACTIVE                                 # §6.7: deliberately soft


@pytest.mark.django_db
def test_the_sweep_is_idempotent_it_raises_one_flag_however_often_it_runs():
    adopter = person()
    for i in (30, 20, 10):
        _inquiry(adopter, minutes=i)
    for _ in range(3):
        offenders.raise_repeat_withdrawal_flags()
    assert ModerationFlag.objects.filter(rule=offenders.RULE_WITHDRAWAL).count() == 1


@pytest.mark.django_db
def test_an_adoption_resets_the_run():
    adopter = person()
    _inquiry(adopter, minutes=50)
    _inquiry(adopter, minutes=40)
    _inquiry(adopter, status=InquiryStatus.ADOPTED, end_reason="", minutes=30)       # adopted: consecutive resets
    _inquiry(adopter, minutes=20)
    _inquiry(adopter, minutes=10)
    assert offenders.raise_repeat_withdrawal_flags() == []


@pytest.mark.django_db
def test_only_the_adopters_own_withdrawals_count():
    """A listing taken down closes inquiries as withdrawn by the SYSTEM, and a poster can decline; neither is the adopter backing out."""
    adopter = person()
    for i in (30, 20, 10):
        _inquiry(adopter, end_reason=EndReason.LISTING_WITHDRAWN, minutes=i)
    _inquiry(adopter, status=InquiryStatus.DECLINED, end_reason=EndReason.NOT_A_FIT, minutes=5)
    assert offenders.raise_repeat_withdrawal_flags() == []


@pytest.mark.django_db
def test_a_dismissed_withdrawal_flag_is_not_raised_again_by_the_next_sweep_only_by_three_more():
    adopter = person()
    for i in (60, 50, 40):
        _inquiry(adopter, minutes=i)
    first = offenders.raise_repeat_withdrawal_flags()[0]
    resolve_flag(first, AccountFactory(), FlagStatus.DISMISSED)
    assert offenders.raise_repeat_withdrawal_flags() == []                           # same three: nothing new
    for i in (-1, -2):                                                              # decided AFTER the flag was raised
        _inquiry(adopter, minutes=i)
    assert offenders.raise_repeat_withdrawal_flags() == []                           # two more is not three
    _inquiry(adopter, minutes=-3)
    assert len(offenders.raise_repeat_withdrawal_flags()) == 1


@pytest.mark.django_db
def test_the_sweep_runs_hourly_with_the_others():
    from sagip.management.commands.run_sweeps import SWEEPS
    assert "flags_repeat_withdrawals" in [label for label, _ in SWEEPS]


# ---- the gaps the mutation run found ------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_six_takedowns_while_the_first_flag_is_still_open_still_make_one_flag():
    """Three MORE takedowns would satisfy the 'three more' rule, but a person is only asked to look at one flag at a time."""
    poster = person()
    _take_down(poster, 6)
    assert ModerationFlag.objects.filter(rule=offenders.RULE_TAKEDOWN).count() == 1


@pytest.mark.django_db
def test_withdrawals_the_system_or_the_poster_made_in_between_do_not_count_and_do_not_break_the_run():
    adopter = person()
    _inquiry(adopter, minutes=60)
    _inquiry(adopter, end_reason=EndReason.LISTING_WITHDRAWN, minutes=50)             # a takedown closed it: not the adopter backing out
    _inquiry(adopter, minutes=40)
    _inquiry(adopter, status=InquiryStatus.DECLINED, end_reason=EndReason.NOT_A_FIT, minutes=30)
    _inquiry(adopter, minutes=20)
    [flag] = offenders.raise_repeat_withdrawal_flags()
    assert "3 adoption inquiries withdrawn in a row" in flag.reason                  # three of their own, not five rows


@pytest.mark.django_db
def test_a_flag_about_their_message_or_their_volunteer_request_is_on_their_record():
    from django.utils import timezone

    from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup
    from volunteer.tests.helpers import verified_shelter
    adopter, volunteer = person(), person()
    inquiry = _inquiry(adopter)
    shift = VolunteerShift.objects.create(shelter_account=verified_shelter(), type="walking", capacity=2, status="open",
                                          starts_at=timezone.now() + timezone.timedelta(days=2), ends_at=timezone.now() + timezone.timedelta(days=2, hours=2))
    signup = VolunteerSignup.objects.create(shift=shift, volunteer_account=volunteer, status=SignupStatus.REQUESTED)
    about_message, about_request = _flag("inquiry", inquiry.pk), _flag("signup", signup.pk)
    assert list(ModerationFlag.objects.filter(offenders.about_person(adopter))) == [about_message]
    assert list(ModerationFlag.objects.filter(offenders.about_person(volunteer))) == [about_request]
