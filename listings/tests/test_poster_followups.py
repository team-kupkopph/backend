"""Backend follow-ups for the app's poster screens (spec 2026-10-06 §5)."""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from listings.models import AdoptionInquiry, AdoptionStage
from listings.tests.adoption_helpers import c, inquire, listing, person, post, shelter
from notifications.models import Notification


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


def _screened(poster, the_listing, member):
    adopter = person(member=member)
    inq = inquire(adopter, the_listing)
    assert post(poster, inq, "screen").status_code == 200
    return adopter, inq


@pytest.mark.django_db
def test_badge_needed_is_sent_at_most_once_a_day():
    poster = shelter(); milo = listing(poster, name="Milo")
    adopter, inq = _screened(poster, milo, member=False)
    for _ in range(3):
        assert post(poster, inq, "reserve").json()["error"]["code"] == "adopter_badge_required"
    assert Notification.objects.filter(account=adopter, type="adoption_badge_needed").count() == 1
    Notification.objects.filter(account=adopter, type="adoption_badge_needed").update(
        created_at=timezone.now() - timezone.timedelta(hours=25))
    post(poster, inq, "reserve")
    assert Notification.objects.filter(account=adopter, type="adoption_badge_needed").count() == 2


@pytest.mark.django_db
def test_a_stage_move_without_a_note_clears_the_old_note():
    poster = person(member=True); milo = listing(poster, name="Milo")
    _, inq = _screened(poster, milo, member=True)
    home = AdoptionStage.objects.get(inquiry=inq, stage_key="home_check")
    assert home.note == "Not needed when adopting from an individual."      # AQ5 pre-skip
    c(poster).post(f"/api/v1/inquiries/{inq.pk}/stages/home_check", {"state": "in_progress"}, format="json")
    home.refresh_from_db()
    assert home.state == "in_progress" and home.note == ""
    c(poster).post(f"/api/v1/inquiries/{inq.pk}/stages/home_check",
                   {"state": "done", "note": "Lovely yard"}, format="json")
    home.refresh_from_db()
    assert home.note == "Lovely yard"


@pytest.mark.django_db
def test_reserving_again_clears_reservation_released():
    poster = shelter(); milo = listing(poster, name="Milo")
    _, inq = _screened(poster, milo, member=True)
    post(poster, inq, "reserve"); post(poster, inq, "unreserve")
    fin = AdoptionStage.objects.get(inquiry=inq, stage_key="finalization")
    assert fin.note == "Reservation released"
    post(poster, inq, "reserve")
    fin.refresh_from_db()
    assert fin.state == "in_progress" and fin.note == ""
