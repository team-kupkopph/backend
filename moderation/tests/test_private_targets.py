"""Review step 7c/7e · the two private things one person can show another: an adopter's inquiry message to a poster, and a
volunteer's request as a shelter sees it. Each is reportable, and the person behind it can be reported or blocked, but only
by the one it was shown to. Anyone else gets the same 404 a missing thing gets, so a private id cannot be probed.

Found by reading the code, not by the review: a volunteer's signup `notes` column is never written (no endpoint accepts
it), so there is no volunteer note to report. The free text that exists between users is the inquiry message and the
adopter's quick answers; the signup is reportable as a way to reach the volunteer, not for any text it holds."""
import uuid

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from listings.tests.adoption_helpers import c, inquire, listing, person
from moderation import targets
from moderation.models import AccountBlock, FlagTarget, ModerationFlag
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.tests.helpers import verified_shelter

FLAGS, BLOCKS = "/api/v1/moderation/flags", "/api/v1/me/blocks"


def _signup(shelter, volunteer):
    shift = VolunteerShift.objects.create(
        shelter_account=shelter, type="walking", capacity=2, status="open", title="Morning dog walk",
        starts_at=timezone.now() + timezone.timedelta(days=2), ends_at=timezone.now() + timezone.timedelta(days=2, hours=2))
    return VolunteerSignup.objects.create(shift=shift, volunteer_account=volunteer, status=SignupStatus.REQUESTED)


@pytest.fixture
def inquiry():
    poster, adopter = person(), person()
    return inquire(adopter, listing(poster), message="I can pay a deposit outside the app"), poster, adopter


# ---- an inquiry message ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_poster_can_report_an_inquiry_message(inquiry):
    i, poster, adopter = inquiry
    res = c(poster).post(FLAGS, {"target_type": "inquiry", "target_id": str(i.pk), "reason_code": "spam_scam"}, format="json")
    assert res.status_code == 201, res.content
    assert ModerationFlag.objects.get().target_type == FlagTarget.INQUIRY


@pytest.mark.django_db
def test_nobody_else_can_report_it_and_the_answer_is_a_plain_404(inquiry):
    i, poster, adopter = inquiry
    for who in (adopter, person()):                       # not even the one who wrote it, and not a stranger
        res = c(who).post(FLAGS, {"target_type": "inquiry", "target_id": str(i.pk), "reason_code": "spam_scam"}, format="json")
        assert res.status_code == 404
    missing = c(poster).post(FLAGS, {"target_type": "inquiry", "target_id": str(uuid.uuid4()), "reason_code": "spam_scam"},
                             format="json")
    assert missing.status_code == 404 and missing.json()["error"]["message"] == res.json()["error"]["message"]  # and it reads exactly like a message that is not there
    assert ModerationFlag.objects.count() == 0


@pytest.mark.django_db
def test_the_moderator_is_shown_the_adopters_words_and_answers():
    poster, adopter = person(), person()
    the_listing = listing(poster)
    res = c(adopter).post(f"/api/v1/listings/{the_listing.pk}/inquiries",
                          {"message": "Hello", "application": {"other_pets": "two cats", "schedule": "home all day"}},
                          format="json")
    seen = targets.describe_target(FlagTarget.INQUIRY, res.json()["inquiry_id"])
    assert seen["state"] == "present" and seen["label"] == f"Message from {adopter.display_name}"
    assert seen["body"] == "Hello\nOther pets: two cats\nSchedule: home all day"
    assert seen["owner"]["account_id"] == str(adopter.pk) and seen["on_action"]["label"] == "Mark handled"


@pytest.mark.django_db
def test_the_poster_can_report_and_block_the_adopter_through_the_message(inquiry):
    i, poster, adopter = inquiry
    flag = c(poster).post(FLAGS, {"target_type": "account", "via_type": "inquiry", "via_id": str(i.pk),
                                  "reason_code": "harassment"}, format="json")
    assert flag.status_code == 201 and ModerationFlag.objects.get().target_id == adopter.pk
    block = c(poster).post(BLOCKS, {"via_type": "inquiry", "via_id": str(i.pk)}, format="json")
    assert block.status_code == 201 and AccountBlock.objects.get().blocked_id == adopter.pk
    assert str(adopter.pk) not in str(block.json())       # the id never comes back


@pytest.mark.django_db
def test_a_stranger_cannot_use_a_message_to_find_or_block_its_author(inquiry):
    i, poster, adopter = inquiry
    for who in (adopter, person()):
        assert c(who).post(BLOCKS, {"via_type": "inquiry", "via_id": str(i.pk)}, format="json").status_code == 404
        assert c(who).post(FLAGS, {"target_type": "account", "via_type": "inquiry", "via_id": str(i.pk),
                                   "reason_code": "harassment"}, format="json").status_code == 404
    assert AccountBlock.objects.count() == 0 and ModerationFlag.objects.count() == 0


# ---- a volunteer's request ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_shelter_can_report_and_block_a_volunteer_by_their_request():
    shelter, volunteer = verified_shelter(), person()
    signup = _signup(shelter, volunteer)
    flag = c(shelter).post(FLAGS, {"target_type": "account", "via_type": "signup", "via_id": str(signup.pk),
                                   "reason_code": "harassment"}, format="json")
    assert flag.status_code == 201 and ModerationFlag.objects.get().target_id == volunteer.pk
    block = c(shelter).post(BLOCKS, {"via_type": "signup", "via_id": str(signup.pk)}, format="json")
    assert block.status_code == 201 and AccountBlock.objects.get().blocked_id == volunteer.pk
    assert str(volunteer.pk) not in str(block.json())


@pytest.mark.django_db
def test_only_the_shelter_that_owns_the_shift_can_do_it():
    shelter, volunteer = verified_shelter(), person()
    signup = _signup(shelter, volunteer)
    other_shelter = verified_shelter()
    for who in (volunteer, other_shelter, person()):
        assert c(who).post(BLOCKS, {"via_type": "signup", "via_id": str(signup.pk)}, format="json").status_code == 404
        assert c(who).post(FLAGS, {"target_type": "signup", "target_id": str(signup.pk), "reason_code": "other",
                                   "reason": "x"}, format="json").status_code == 404
    assert AccountBlock.objects.count() == 0 and ModerationFlag.objects.count() == 0


@pytest.mark.django_db
def test_a_signup_is_described_by_who_and_what_without_any_free_text():
    shelter, volunteer = verified_shelter(), person()
    signup = _signup(shelter, volunteer)
    seen = targets.describe_target(FlagTarget.SIGNUP, signup.pk)
    assert seen["label"] == f"{volunteer.display_name} · Morning dog walk" and seen["body"] == ""


# ---- the registry stays honest -----------------------------------------------------------------------
def test_both_are_in_the_via_list_and_neither_has_a_takedown():
    assert FlagTarget.INQUIRY in targets.VIA_TYPES and FlagTarget.SIGNUP in targets.VIA_TYPES
    for kind in (FlagTarget.INQUIRY, FlagTarget.SIGNUP):
        spec = targets.REGISTRY[kind]
        assert spec.take_down is None and spec.reporter_may is not None and spec.person is not None


@pytest.mark.django_db
def test_every_public_target_stays_open_to_any_signed_in_reporter():
    """Only the two private types are restricted. A listing, story or shift is public, so reporting it is not gated."""
    assert [k for k, s in targets.REGISTRY.items() if s.reporter_may is not None] == [FlagTarget.INQUIRY, FlagTarget.SIGNUP]
    stranger = AccountFactory()
    live = listing(person())
    assert targets.target_exists("listing", live.pk, stranger) is True
