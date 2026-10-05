"""AQ1 · accepting an applicant for screening shares both phones and starts the application;
AD3 · stage moves only on an open, public inquiry the poster has accepted."""
import uuid

import pytest

from listings.models import AdoptionInquiry, AdoptionStage
from listings.tests.adoption_helpers import c, inquire, listing, person, post, shelter, types


def _stage(inq, key):
    return AdoptionStage.objects.get(inquiry=inq, stage_key=key)


@pytest.mark.django_db
def test_screen_accepts_starts_the_application_and_tells_the_adopter():
    poster, adopter = shelter(), person()
    inq = inquire(adopter, listing(poster, name="Milo"))
    res = post(poster, inq, "screen")
    assert res.status_code == 200
    body = res.json()
    assert body["accepted_at"] is not None and body["adopter_contact"]["phone"] == adopter.phone
    assert _stage(inq, "application").state == "in_progress"
    assert types(adopter) == ["inquiry_accepted"]
    n = adopter.notifications.get(type="inquiry_accepted")
    assert n.title == "Paws Marikina wants to get to know you"
    assert n.body == "They accepted your inquiry about Milo. You can now see each other's phone numbers."


@pytest.mark.django_db
@pytest.mark.parametrize("who,code,status", [("stranger", "not_your_listing", 403),
                                             ("adopter", "not_your_listing", 403)])
def test_only_the_poster_can_screen(who, code, status):
    poster, adopter = shelter(), person()
    inq = inquire(adopter, listing(poster))
    caller = adopter if who == "adopter" else person()
    res = post(caller, inq, "screen")
    assert res.status_code == status and res.json()["error"]["code"] == code


@pytest.mark.django_db
def test_screen_refusals():
    poster, adopter = shelter(), person()
    inq = inquire(adopter, listing(poster))
    assert post(poster, inq, "screen").status_code == 200
    again = post(poster, inq, "screen")
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_screening"
    AdoptionInquiry.objects.filter(pk=inq.pk).update(status="withdrawn")
    closed = post(poster, inq, "screen")
    assert closed.status_code == 409 and closed.json()["error"]["code"] == "inquiry_closed"
    missing = c(poster).post(f"/api/v1/inquiries/{uuid.uuid4()}/screen", {}, format="json")
    assert missing.status_code == 404


@pytest.mark.django_db
def test_a_poster_with_no_phone_to_share_cannot_screen():
    poster = person(member=True, phone=False)
    inq = inquire(person(), listing(poster))
    res = post(poster, inq, "screen")
    assert res.status_code == 409 and res.json()["error"]["code"] == "poster_phone_required"
    assert AdoptionInquiry.objects.get(pk=inq.pk).accepted_at is None


@pytest.mark.django_db
def test_a_placement_is_not_screened():
    poster, recipient = person(member=True), person(member=True)
    inq = AdoptionInquiry.objects.create(listing=listing(poster, status="pending"),
                                         adopter_account=recipient, kind="placement")
    res = post(poster, inq, "screen")
    assert res.status_code == 409 and res.json()["error"]["code"] == "is_placement"


def _move(poster, inq, key, state):
    return c(poster).post(f"/api/v1/inquiries/{inq.pk}/stages/{key}", {"state": state}, format="json")


@pytest.mark.django_db
def test_stage_moves_need_screening_first_and_an_open_inquiry():
    poster, adopter = shelter(), person()
    inq = inquire(adopter, listing(poster))
    early = _move(poster, inq, "interview", "in_progress")
    assert early.status_code == 409 and early.json()["error"]["code"] == "not_screening"
    post(poster, inq, "screen")
    assert _move(poster, inq, "interview", "in_progress").status_code == 200
    AdoptionInquiry.objects.filter(pk=inq.pk).update(status="declined")
    late = _move(poster, inq, "interview", "done")
    assert late.status_code == 409 and late.json()["error"]["code"] == "inquiry_closed"


@pytest.mark.django_db
def test_the_inquiry_step_is_locked_and_finalization_done_needs_complete():
    poster, adopter = shelter(), person()
    inq = inquire(adopter, listing(poster)); post(poster, inq, "screen")
    locked = _move(poster, inq, "inquiry", "skipped")
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "stage_locked"
    done = _move(poster, inq, "finalization", "done")
    assert done.status_code == 409 and done.json()["error"]["code"] == "use_complete"
    assert _move(poster, inq, "finalization", "in_progress").status_code == 200
