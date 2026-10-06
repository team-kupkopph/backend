"""Backend follow-ups for the app's poster screens (spec 2026-10-06 §5)."""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from listings.models import AdoptionInquiry
from listings.tests.adoption_helpers import c, inquire, listing, person, shelter


@pytest.mark.django_db
def test_mine_cards_count_open_applicants_only():
    poster = shelter(); milo = listing(poster, name="Milo"); luna = listing(poster, name="Luna")
    inquire(person(), milo); inquire(person(), milo)
    closed = inquire(person(), milo)
    AdoptionInquiry.objects.filter(pk=closed.pk).update(status="declined")
    AdoptionInquiry.objects.create(listing=luna, adopter_account=person(), kind="placement")
    rows = c(poster).get("/api/v1/listings?mine=true&status=available").json()["results"]
    counts = {r["pet"]["name"]: r["open_inquiries"] for r in rows}
    assert counts == {"Milo": 2, "Luna": 0}


@pytest.mark.django_db
def test_the_public_feed_does_not_carry_the_count():
    milo = listing(shelter(), name="Milo"); inquire(person(), milo)
    rows = c(person()).get("/api/v1/listings").json()["results"]
    assert rows and "open_inquiries" not in rows[0]


@pytest.mark.django_db
def test_the_count_is_one_aggregate_not_a_query_per_card():
    """_card already costs a photo query per card (pre-existing); the count must add no more than
    the one annotated listing query — so no query beyond it may touch adoption_inquiry."""
    poster = shelter()
    for i in range(6):
        inquire(person(), listing(poster, name=f"Pet{i}"))
    with CaptureQueriesContext(connection) as ctx:
        assert len(c(poster).get("/api/v1/listings?mine=true&status=available").json()["results"]) == 6
    touching = [q for q in ctx.captured_queries if '"adoption_inquiry"' in q["sql"]]
    assert len(touching) <= 1
