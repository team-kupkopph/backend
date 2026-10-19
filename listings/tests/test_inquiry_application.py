"""AD8 · the inquiry carries a message and a few structured answers the poster screens with."""
import pytest

from listings.tests.adoption_helpers import c, inquire, listing, person

ANSWERS = {"home_type": "apartment", "rents": True, "landlord_ok": True,
           "other_pets": "One cat, 4 years", "experience": "Had dogs all my life",
           "schedule": "Work from home Mon–Thu"}


def _ask(adopter, item, body):
    return c(adopter).post(f"/api/v1/listings/{item.pk}/inquiries", body, format="json")


@pytest.mark.django_db
def test_the_application_is_stored_and_the_poster_reads_it():
    poster, adopter = person(), person()
    item = listing(poster)
    res = _ask(adopter, item, {"message": "Hi!", "application": ANSWERS})
    assert res.status_code == 201, res.content
    [row] = c(poster).get(f"/api/v1/listings/{item.pk}/inquiries").json()["results"]
    assert row["application"] == ANSWERS
    assert c(poster).get(f"/api/v1/inquiries/{res.json()['inquiry_id']}").json()["application"] == ANSWERS


@pytest.mark.django_db
def test_an_inquiry_without_an_application_still_works_and_reads_null():
    poster, adopter = person(), person()
    item = listing(poster)
    inq = inquire(adopter, item)
    assert c(poster).get(f"/api/v1/inquiries/{inq.pk}").json()["application"] is None


@pytest.mark.django_db
def test_unknown_keys_are_dropped_and_a_bad_home_type_is_refused():
    poster, adopter, other = person(), person(), person()
    item = listing(poster)
    assert _ask(adopter, item, {"application": {"home_type": "castle"}}).status_code == 400
    res = _ask(other, item, {"application": {"home_type": "house", "ssn": "123"}})
    assert res.status_code == 201
    row = c(poster).get(f"/api/v1/inquiries/{res.json()['inquiry_id']}").json()
    assert row["application"] == {"home_type": "house"}


@pytest.mark.django_db
def test_free_text_answers_are_length_capped():
    adopter = person()
    item = listing(person())
    assert _ask(adopter, item, {"application": {"experience": "x" * 501}}).status_code == 400


@pytest.mark.django_db
def test_the_adopter_row_does_not_echo_the_application_to_anyone_else():
    poster, adopter = person(), person()
    item = listing(poster)
    inq = inquire(adopter, item)
    assert "application" not in c(adopter).get(f"/api/v1/inquiries/{inq.pk}").json()
