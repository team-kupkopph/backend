"""Review plan rows U4, U5, U7, U8 (Phase 4) · the edges of what the moderation endpoints and `resolve_flag` accept.

Written as a regression net, not a fix: each row asserts the refusal is a clean 4xx in the error envelope (or a ModerationError),
that nothing was written, and that nothing 500s."""
import uuid

import pytest

from accounts.factories import AccountFactory
from listings.tests.adoption_helpers import c, listing, person
from moderation.actions import ModerationError, resolve_flag
from moderation.models import FlagStatus, ModerationFlag

FLAGS = "/api/v1/moderation/flags"


def _flag():
    return ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing",
                                         target_id=listing(person()).pk, reason="x")


# ---- U4 · resolve_flag's status ---------------------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("bad", [None, "", "open", "unheard-of", FlagStatus.OPEN])
def test_resolve_flag_refuses_a_status_that_is_not_a_decision_and_writes_nothing(bad):
    flag = _flag()
    with pytest.raises(ModerationError):
        resolve_flag(flag, AccountFactory(), bad)
    flag.refresh_from_db()
    assert flag.status == FlagStatus.OPEN and flag.reviewed_by_id is None and flag.first_reviewed_at is None


# ---- U5 · resolve_flag's reviewer -------------------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("status", [FlagStatus.REVIEWED, FlagStatus.ACTIONED, FlagStatus.DISMISSED])
def test_an_anonymous_decision_is_refused_before_anything_happens(status):
    the_listing = listing(person())
    flag = ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing", target_id=the_listing.pk, reason="x")
    with pytest.raises(ModerationError):
        resolve_flag(flag, None, status)
    flag.refresh_from_db()
    the_listing.refresh_from_db()
    assert flag.status == FlagStatus.OPEN and the_listing.hidden_at is None            # no side effect ran


# ---- U7 · the note's boundaries ----------------------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("note, expected", [
    ("a", 201), ("x" * 2000, 201), ("x" * 2001, 400), ("héllo 🐕 שלום", 201),
])
def test_the_note_length_boundaries(note, expected):
    target = listing(person())
    res = c(person()).post(FLAGS, {"target_type": "listing", "target_id": str(target.pk), "reason_code": "other", "reason": note},
                           format="json")
    assert res.status_code == expected, res.content
    assert ModerationFlag.objects.count() == (1 if expected == 201 else 0)


@pytest.mark.django_db
@pytest.mark.parametrize("note", ["", "   ", "\n\t "])
def test_something_else_with_no_words_is_refused_but_a_category_with_no_words_is_not(note):
    target = listing(person())
    refused = c(person()).post(FLAGS, {"target_type": "listing", "target_id": str(target.pk), "reason_code": "other", "reason": note},
                               format="json")
    assert refused.status_code == 400 and refused.json()["error"]["field"] == "reason"
    ok = c(person()).post(FLAGS, {"target_type": "listing", "target_id": str(target.pk), "reason_code": "spam_scam", "reason": note},
                          format="json")
    assert ok.status_code == 201


@pytest.mark.django_db
@pytest.mark.parametrize("note", [["a"], {"a": 1}, True])
def test_a_note_that_is_not_text_is_a_400_not_a_500(note):
    target = listing(person())
    res = c(person()).post(FLAGS, {"target_type": "listing", "target_id": str(target.pk), "reason_code": "other", "reason": note},
                           format="json")
    assert res.status_code == 400 and res.json()["error"]["code"]
    assert ModerationFlag.objects.count() == 0


@pytest.mark.django_db
def test_a_nul_byte_in_the_note_is_refused_cleanly_not_a_database_error():
    """Postgres cannot store NUL in text; it must not surface as a 500 after the request is accepted."""
    target = listing(person())
    res = c(person()).post(FLAGS, {"target_type": "listing", "target_id": str(target.pk), "reason_code": "spam_scam",
                                   "reason": "bad\u0000byte"}, format="json")
    assert res.status_code in (201, 400), res.content
    if res.status_code == 201:
        assert "\u0000" not in ModerationFlag.objects.get().reason


@pytest.mark.django_db
def test_a_bare_number_as_the_note_is_read_as_its_digits():
    """DRF's CharField accepts a number and stores its text. Harmless, and pinned so nobody is surprised by it."""
    target = listing(person())
    res = c(person()).post(FLAGS, {"target_type": "listing", "target_id": str(target.pk), "reason_code": "other", "reason": 123},
                           format="json")
    assert res.status_code == 201 and ModerationFlag.objects.get().reason == "123"


# ---- U8 · the shape of the request -------------------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("body", [
    [], None, "text", 5,
    {}, {"target_type": "listing"}, {"target_id": str(uuid.uuid4())},
    {"target_type": "listing", "target_id": "not-a-uuid", "reason_code": "spam_scam"},
    {"target_type": "listing", "target_id": 12345, "reason_code": "spam_scam"},
    {"target_type": "spaceship", "target_id": str(uuid.uuid4()), "reason_code": "spam_scam"},
    {"target_type": ["listing"], "target_id": str(uuid.uuid4()), "reason_code": "spam_scam"},
    {"target_type": "listing", "target_id": str(uuid.uuid4()), "reason_code": "nonsense"},
])
def test_a_malformed_request_is_a_4xx_in_the_envelope_with_no_row(body):
    res = c(person()).post(FLAGS, body, format="json")
    assert 400 <= res.status_code < 500, (body, res.status_code, res.content)
    assert "error" in res.json()
    assert ModerationFlag.objects.count() == 0


@pytest.mark.django_db
def test_a_flag_on_the_unbuilt_message_type_is_accepted_today_and_that_is_recorded_here():
    """MO29: `message` has no model behind it, so any UUID is accepted. Pinned so a change is a decision, not a surprise."""
    res = c(person()).post(FLAGS, {"target_type": "message", "target_id": str(uuid.uuid4()), "reason_code": "harassment"}, format="json")
    assert res.status_code == 201
