"""AD1/AD2 · the poster reads their applicants; AQ1 · each side sees the other's phone only once
the poster has accepted the applicant for screening."""
import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from listings.models import AdoptionInquiry
from listings.tests.adoption_helpers import c, inquire, listing, person, shelter


@pytest.mark.django_db
def test_the_poster_reads_an_inquiry_on_their_listing():
    poster, adopter = shelter(), person(city="Pasig City")
    inq = inquire(adopter, listing(poster), message="We have a yard.")
    body = c(poster).get(f"/api/v1/inquiries/{inq.pk}").json()
    assert body["viewer"] == "poster" and body["kind"] == "inquiry" and body["status"] == "active"
    assert body["message"] == "We have a yard."
    assert body["adopter"] == {"account_id": str(adopter.pk), "display_name": adopter.display_name,
                               "city": "Pasig City", "verified_member": False}
    assert [s["stage_key"] for s in body["stages"]][0] == "inquiry"
    assert "adopter_contact" not in body                       # not accepted yet (AQ1)


@pytest.mark.django_db
def test_the_adopter_tier_is_unchanged_in_shape_and_gains_the_new_fields():
    poster, adopter = shelter(), person(member=True)
    inq = inquire(adopter, listing(poster))
    body = c(adopter).get(f"/api/v1/inquiries/{inq.pk}").json()
    assert body["viewer"] == "adopter" and body["verified_member"] is True
    assert body["accepted_at"] is None and body["reserved_at"] is None and body["end_reason"] is None
    assert "poster_contact" not in body and "adopter" not in body and "message" not in body
    listed = c(adopter).get("/api/v1/me/inquiries").json()["results"][0]
    assert listed == body                                       # one row builder, two endpoints


@pytest.mark.django_db
def test_a_stranger_gets_404_not_403():
    inq = inquire(person(), listing(shelter()))
    assert c(person()).get(f"/api/v1/inquiries/{inq.pk}").status_code == 404
    assert c(person()).get(f"/api/v1/inquiries/{uuid.uuid4()}").status_code == 404


@pytest.mark.django_db
def test_aq1_contact_is_revealed_both_ways_once_accepted_and_stays_on_adoption():
    poster, adopter = shelter(), person()
    inq = inquire(adopter, listing(poster))
    AdoptionInquiry.objects.filter(pk=inq.pk).update(accepted_at=timezone.now())
    as_poster = c(poster).get(f"/api/v1/inquiries/{inq.pk}").json()
    as_adopter = c(adopter).get(f"/api/v1/inquiries/{inq.pk}").json()
    assert as_poster["adopter_contact"] == {"name": adopter.display_name, "phone": adopter.phone}
    assert as_adopter["poster_contact"] == {"name": "Ana Cruz", "phone": "+63281234567"}
    AdoptionInquiry.objects.filter(pk=inq.pk).update(status="adopted")
    assert "adopter_contact" in c(poster).get(f"/api/v1/inquiries/{inq.pk}").json()
    AdoptionInquiry.objects.filter(pk=inq.pk).update(status="declined")
    assert "adopter_contact" not in c(poster).get(f"/api/v1/inquiries/{inq.pk}").json()
    assert "poster_contact" not in c(adopter).get(f"/api/v1/inquiries/{inq.pk}").json()


@pytest.mark.django_db
def test_an_individual_posters_contact_is_their_verified_phone():
    poster, adopter = person(member=True), person()
    inq = inquire(adopter, listing(poster))
    AdoptionInquiry.objects.filter(pk=inq.pk).update(accepted_at=timezone.now())
    body = c(adopter).get(f"/api/v1/inquiries/{inq.pk}").json()
    assert body["poster_contact"] == {"name": poster.display_name, "phone": poster.phone}


@pytest.mark.django_db
def test_the_poster_sees_only_a_verified_adopter_phone():
    """AQ1 says the adopter's *verified* phone: the phone-change flow clears phone_verified_at, so
    an unverified number must not reach the poster. The contact card stays, with no number."""
    poster, adopter = shelter(), person()
    inq = inquire(adopter, listing(poster))
    AdoptionInquiry.objects.filter(pk=inq.pk).update(accepted_at=timezone.now())
    url = f"/api/v1/inquiries/{inq.pk}"
    assert c(poster).get(url).json()["adopter_contact"]["phone"] == adopter.phone
    adopter.phone = "+639179999999"; adopter.phone_verified_at = None
    adopter.save(update_fields=["phone", "phone_verified_at"])
    for body in (c(poster).get(url).json(),
                 c(poster).get(f"/api/v1/listings/{inq.listing_id}/inquiries").json()["results"][0]):
        assert body["adopter_contact"] == {"name": adopter.display_name, "phone": None}


@pytest.mark.django_db
def test_the_applicants_list_does_not_confirm_a_hidden_listing_to_a_stranger():
    """A draft or an AD16-hidden listing doesn't exist to anyone but its poster: 404, not 403.
    A public listing still answers a non-poster 403 not_your_listing."""
    draft = listing(shelter(), status="draft")
    unverified = listing(person(), verify=False)
    public = listing(shelter())
    for hidden in (draft, unverified):
        res = c(person()).get(f"/api/v1/listings/{hidden.pk}/inquiries")
        assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"
        assert c(hidden.posted_by).get(f"/api/v1/listings/{hidden.pk}/inquiries").status_code == 200
    res = c(person()).get(f"/api/v1/listings/{public.pk}/inquiries")
    assert res.status_code == 403 and res.json()["error"]["code"] == "not_your_listing"


@pytest.mark.django_db
def test_the_applicants_list_is_the_posters_only_and_filters_open():
    poster = shelter(); the_listing = listing(poster)
    a, b = inquire(person(), the_listing), inquire(person(), the_listing)
    AdoptionInquiry.objects.filter(pk=a.pk).update(status="declined")
    url = f"/api/v1/listings/{the_listing.pk}/inquiries"
    every = c(poster).get(url).json()
    assert [r["inquiry_id"] for r in every["results"]] == [str(b.pk), str(a.pk)]   # newest first
    assert every["next"] is None and every["results"][0]["viewer"] == "poster"
    open_only = c(poster).get(url + "?status=open").json()["results"]
    assert [r["inquiry_id"] for r in open_only] == [str(b.pk)]
    res = c(person()).get(url)
    assert res.status_code == 403 and res.json()["error"]["code"] == "not_your_listing"
    assert c(poster).get(f"/api/v1/listings/{uuid.uuid4()}/inquiries").status_code == 404


def _queries_for(client, url):
    with CaptureQueriesContext(connection) as ctx:
        res = client.get(url)
    assert res.status_code == 200, res.content
    return len(ctx), res.json()["results"]


@pytest.mark.django_db
def test_the_applicants_list_costs_the_same_for_two_rows_as_for_six():
    poster = shelter(); the_listing = listing(poster)
    for _ in range(2):
        inquire(person(), the_listing)
    two, rows = _queries_for(c(poster), f"/api/v1/listings/{the_listing.pk}/inquiries")
    assert len(rows) == 2
    for _ in range(4):
        inquire(person(), the_listing)
    AdoptionInquiry.objects.filter(listing=the_listing).update(accepted_at=timezone.now())
    six, rows = _queries_for(c(poster), f"/api/v1/listings/{the_listing.pk}/inquiries")
    assert len(rows) == 6 and all("adopter_contact" in r for r in rows)   # the reveal is costed
    assert six == two


@pytest.mark.django_db
def test_my_inquiries_costs_the_same_for_one_row_as_for_six_across_posters():
    adopter = person()
    first = inquire(adopter, listing(shelter()))
    AdoptionInquiry.objects.filter(pk=first.pk).update(accepted_at=timezone.now())
    one, rows = _queries_for(c(adopter), "/api/v1/me/inquiries")
    assert len(rows) == 1 and "poster_contact" in rows[0]
    posters = [shelter(), shelter(), person(), person(), shelter()]       # shelters and individuals
    for poster in posters:
        inq = inquire(adopter, listing(poster))
        AdoptionInquiry.objects.filter(pk=inq.pk).update(accepted_at=timezone.now())
    six, rows = _queries_for(c(adopter), "/api/v1/me/inquiries")
    assert len(rows) == 6 and all("poster_contact" in r for r in rows)
    assert six == one
