"""AD23 · an inquiry nobody answers doesn't sit `active` forever: after 7 days the poster is
nudged once; after 21 days it closes with a polite notice. Only unanswered public inquiries."""
import pytest
from django.utils import timezone

from listings.models import AdoptionInquiry, InquiryStatus, ListingStatus
from listings.sweeps import STALE_CLOSE_DAYS, STALE_NUDGE_DAYS, close_stale_inquiries, nudge_stale_inquiries
from listings.tests.adoption_helpers import c, inquire, listing, person, post, types


def _age(inq, days):
    AdoptionInquiry.objects.filter(pk=inq.pk).update(
        created_at=timezone.now() - timezone.timedelta(days=days))
    inq.refresh_from_db()


@pytest.mark.django_db
def test_a_week_old_unanswered_inquiry_nudges_the_poster_once():
    poster, adopter = person(), person()
    inq = inquire(adopter, listing(poster))
    _age(inq, STALE_NUDGE_DAYS - 1)
    assert nudge_stale_inquiries() == []
    _age(inq, STALE_NUDGE_DAYS + 1)
    assert [i.pk for i in nudge_stale_inquiries()] == [inq.pk]
    assert nudge_stale_inquiries() == []                       # once, not every hour
    assert types(poster).count("inquiry_waiting") == 1
    inq.refresh_from_db(); assert inq.nudged_at is not None and inq.status == InquiryStatus.ACTIVE


@pytest.mark.django_db
def test_an_answered_inquiry_is_never_nudged_or_closed():
    poster, adopter = person(), person(member=True)
    item = listing(poster)
    inq = inquire(adopter, item)
    post(poster, inq, "screen")
    _age(inq, STALE_CLOSE_DAYS + 5)
    assert nudge_stale_inquiries() == [] and close_stale_inquiries() == []
    inq.refresh_from_db(); assert inq.status == InquiryStatus.ACTIVE


@pytest.mark.django_db
def test_three_weeks_unanswered_closes_with_a_polite_notice_and_no_actor():
    poster, adopter = person(), person()
    item = listing(poster)
    inq = inquire(adopter, item)
    _age(inq, STALE_CLOSE_DAYS + 1)
    assert [i.pk for i in close_stale_inquiries()] == [inq.pk]
    inq.refresh_from_db()
    assert inq.status == InquiryStatus.WITHDRAWN and inq.end_reason == "inquiry_expired"
    assert inq.ended_by_account is None and inq.decided_at is not None
    assert "inquiry_rejected" in types(adopter)
    assert close_stale_inquiries() == []


@pytest.mark.django_db
def test_a_reserved_for_another_listing_keeps_unanswered_ones_waiting_but_they_still_close_by_age():
    poster, chosen, waiting = person(), person(member=True), person()
    item = listing(poster)
    a, b = inquire(chosen, item), inquire(waiting, item)
    post(poster, a, "screen"); post(poster, a, "reserve")
    _age(b, STALE_CLOSE_DAYS + 1)
    close_stale_inquiries()
    b.refresh_from_db(); item.refresh_from_db()
    assert b.status == InquiryStatus.WITHDRAWN and item.status == ListingStatus.PENDING


@pytest.mark.django_db
def test_placements_and_hidden_or_draft_listings_are_left_alone():
    poster, adopter = person(), person()
    item = listing(poster)
    inq = inquire(adopter, item)
    AdoptionInquiry.objects.filter(pk=inq.pk).update(kind="placement")
    _age(inq, STALE_CLOSE_DAYS + 1)
    assert nudge_stale_inquiries() == [] and close_stale_inquiries() == []


@pytest.mark.django_db
def test_the_sweep_is_registered_with_run_sweeps():
    from sagip.management.commands.run_sweeps import SWEEPS
    labels = [label for label, _ in SWEEPS]
    assert "inquiries_nudged" in labels and "inquiries_closed" in labels
