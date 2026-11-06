"""Review steps 7g and 7c (MO11, MO1, MQ3) · a report says what kind of problem it is, and a PERSON can be reported.

Before: one free-text reason (every story flag stored the literal "reported from story detail", so no report could be
broken down by type and welfare concerns waited behind spam), and the API accepted `target_type: account` that no
screen ever sent. A story author, a volunteer or a shelter is shown without an account id, so a person is also
reportable "by what they posted" and the server resolves who."""
import uuid

import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from community.models import StoryPost
from listings.tests.adoption_helpers import c, listing, person
from moderation.models import FlagStatus, ModerationFlag, ReasonCode
from sagip.models import StrayReport
from volunteer.models import VolunteerShift
from volunteer.tests.helpers import verified_shelter

URL = "/api/v1/moderation/flags"


def _post(account, **body):
    return c(account).post(URL, body, format="json")


def _story(author=None):
    return StoryPost.objects.create(author_account=author or AccountFactory(), story_type="general", caption="x")


def _shift(owner=None):
    return VolunteerShift.objects.create(
        shelter_account=owner or verified_shelter(), type="walking", capacity=2, status="open",
        starts_at=timezone.now() + timezone.timedelta(days=2), ends_at=timezone.now() + timezone.timedelta(days=2, hours=2))


# ---- 7g · a reason category --------------------------------------------------------------------
@pytest.mark.django_db
def test_a_category_alone_is_a_report():
    live = listing(person())
    res = _post(person(), target_type="listing", target_id=str(live.pk), reason_code="spam_scam")
    assert res.status_code == 201, res.content
    flag = ModerationFlag.objects.get(pk=res.json()["flag_id"])
    assert flag.reason_code == ReasonCode.SPAM_SCAM and flag.reason == ""


@pytest.mark.django_db
def test_a_category_keeps_the_optional_note():
    live = listing(person())
    res = _post(person(), target_type="listing", target_id=str(live.pk), reason_code="animal_welfare",
                reason="  the dog looks starved  ")
    flag = ModerationFlag.objects.get(pk=res.json()["flag_id"])
    assert flag.reason_code == "animal_welfare" and flag.reason == "the dog looks starved"


@pytest.mark.django_db
def test_other_needs_a_note_but_the_other_categories_do_not():
    live = listing(person())
    for blank in ({}, {"reason": ""}, {"reason": "   "}):
        res = _post(person(), target_type="listing", target_id=str(live.pk), reason_code="other", **blank)
        assert res.status_code == 400 and res.json()["error"]["field"] == "reason", blank
    assert _post(person(), target_type="listing", target_id=str(live.pk), reason_code="other",
                 reason="something else").status_code == 201


@pytest.mark.django_db
def test_an_older_app_that_sends_only_a_reason_still_works_as_other():
    live = listing(person())
    res = _post(person(), target_type="listing", target_id=str(live.pk), reason="Spam, a fake listing")
    flag = ModerationFlag.objects.get(pk=res.json()["flag_id"])
    assert res.status_code == 201 and flag.reason_code == "other" and flag.reason == "Spam, a fake listing"
    assert _post(person(), target_type="listing", target_id=str(live.pk)).status_code == 400   # neither: refused


@pytest.mark.django_db
def test_an_unknown_category_is_refused():
    live = listing(person())
    res = _post(person(), target_type="listing", target_id=str(live.pk), reason_code="bad_vibes", reason="x")
    assert res.status_code == 400 and res.json()["error"]["field"] == "reason_code"


@pytest.mark.django_db
def test_stories_are_reported_through_the_same_form():
    story = _story()
    assert _post(person(), target_type="story", target_id=str(story.pk), reason_code="misleading").status_code == 201


# ---- 7c · report a person ----------------------------------------------------------------------
@pytest.mark.django_db
def test_a_person_is_reported_by_their_account_id():
    poster = person()
    res = _post(person(), target_type="account", target_id=str(poster.pk), reason_code="harassment")
    flag = ModerationFlag.objects.get(pk=res.json()["flag_id"])
    assert res.status_code == 201 and flag.target_type == "account" and flag.target_id == poster.pk


@pytest.mark.django_db
@pytest.mark.parametrize("make, who", [
    (lambda: (lambda a: (listing(a), a))(person()), "listing"),
    (lambda: (lambda a: (_story(a), a))(AccountFactory()), "story"),
    (lambda: (lambda a: (_shift(a), a))(verified_shelter()), "shift"),
])
def test_a_person_is_reported_by_what_they_posted_without_the_app_knowing_their_id(make, who):
    """A story's author and a shift's shelter are shown without an account id; the server works out who."""
    thing, owner = make()
    res = _post(person(), target_type="account", via_type=who, via_id=str(thing.pk), reason_code="spam_scam")
    flag = ModerationFlag.objects.get(pk=res.json()["flag_id"])
    assert res.status_code == 201 and flag.target_type == "account" and flag.target_id == owner.pk
    assert str(owner.pk) not in str(res.json())            # the account id is never handed back


@pytest.mark.django_db
def test_reporting_a_person_twice_while_open_collapses_to_one_flag():
    poster = person()
    reporter = person()
    first = _post(reporter, target_type="account", target_id=str(poster.pk), reason_code="harassment")
    second = _post(reporter, target_type="account", target_id=str(poster.pk), reason_code="harassment")
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["flag_id"] == second.json()["flag_id"]


@pytest.mark.django_db
def test_you_cannot_report_yourself_by_id_or_by_what_you_posted():
    me = person()
    mine = listing(me)
    assert _post(me, target_type="account", target_id=str(me.pk), reason_code="other", reason="x").status_code == 422
    res = _post(me, target_type="account", via_type="listing", via_id=str(mine.pk), reason_code="spam_scam")
    assert res.status_code == 422 and res.json()["error"]["code"] == "own_account"
    assert not ModerationFlag.objects.filter(target_type="account", status=FlagStatus.OPEN).exists()


@pytest.mark.django_db
def test_the_person_behind_a_missing_thing_is_a_404_and_a_stray_report_is_never_resolved_to_a_person():
    """An anonymous stray report hides its reporter from everyone else; resolving them from it would unmask them."""
    assert _post(person(), target_type="account", via_type="listing", via_id=str(uuid.uuid4()),
                 reason_code="spam_scam").status_code == 404
    report = StrayReport.objects.create(species="dog", condition="injured", geom=Point(121.05, 14.63, srid=4326),
                                        reporter_account=AccountFactory(), is_anonymous=True)
    res = _post(person(), target_type="account", via_type="report", via_id=str(report.pk), reason_code="spam_scam")
    assert res.status_code == 400 and res.json()["error"]["field"] == "via_type"


@pytest.mark.django_db
def test_via_only_means_something_for_an_account_and_needs_both_halves():
    live = listing(person())
    assert _post(person(), target_type="listing", via_type="listing", via_id=str(live.pk),
                 reason_code="spam_scam").status_code == 400
    assert _post(person(), target_type="account", via_type="listing", reason_code="spam_scam").status_code == 400
    assert _post(person(), target_type="account", reason_code="spam_scam").status_code == 400    # no target at all
