"""AD7 / AQ7 · a moderation takedown of a listing: hidden, 404 publicly, open inquiries closed with
notice, the poster told, a staff restore. A reserved or adopted listing is hidden but the adoption
isn't reversed."""
import pytest
from listings.models import AdoptionListing, InquiryStatus, ListingStatus
from listings.moderation import ListingNotHidden, hide_listing, restore_listing
from listings.tests.adoption_helpers import c, inquire, listing, person, post, types
from moderation.actions import resolve_flag
from moderation.models import FlagStatus, FlagTarget, ModerationFlag


def _staff():
    return person()


def _hide(the_listing, by=None):
    hide_listing(AdoptionListing.objects.get(pk=the_listing.pk), by or _staff())
    the_listing.refresh_from_db()


@pytest.mark.django_db
def test_a_hidden_listing_leaves_the_feed_and_detail_but_the_poster_still_reads_it():
    poster, other = person(), person()
    live = listing(poster)
    _hide(live)
    assert live.hidden_at is not None
    assert c(other).get("/api/v1/listings").json()["results"] == []
    assert c(other).get(f"/api/v1/listings/{live.pk}").status_code == 404
    own = c(poster).get(f"/api/v1/listings/{live.pk}")
    assert own.status_code == 200 and own.json()["removed_by_moderation"] is True


@pytest.mark.django_db
def test_a_hidden_listing_takes_no_new_inquiry_and_cannot_be_published_or_edited():
    poster, adopter = person(), person()
    live = listing(poster)
    _hide(live)
    assert c(adopter).post(f"/api/v1/listings/{live.pk}/inquiries", {}, format="json").status_code == 404
    assert c(poster).patch(f"/api/v1/listings/{live.pk}", {"city": "Pasig"}, format="json"
                           ).json()["error"]["code"] == "listing_removed"
    draft = listing(poster, status=ListingStatus.DRAFT)
    _hide(draft)
    assert c(poster).post(f"/api/v1/listings/{draft.pk}/publish").json()["error"]["code"] == "listing_removed"


@pytest.mark.django_db
def test_the_poster_sees_it_marked_removed_on_their_own_cards():
    poster = person()
    live = listing(poster)
    _hide(live)
    [card] = c(poster).get("/api/v1/listings?mine=true&status=available").json()["results"]
    assert card["removed_by_moderation"] is True


@pytest.mark.django_db
def test_open_inquiries_close_with_a_notice_and_the_poster_is_told():
    poster, a, b = person(), person(), person()
    live = listing(poster)
    ia, ib = inquire(a, live), inquire(b, live)
    _hide(live)
    for inq in (ia, ib):
        inq.refresh_from_db()
        assert inq.status == InquiryStatus.WITHDRAWN and inq.end_reason == "listing_withdrawn"
        assert inq.ended_by_account is None            # the system, not the poster
    assert "listing_withdrawn" in types(a) and "listing_withdrawn" in types(b)
    assert "listing_removed" in types(poster)


@pytest.mark.django_db
def test_a_reserved_listing_is_hidden_but_its_reservation_stands():
    poster, adopter, waiting = person(), person(member=True), person()
    live = listing(poster)
    chosen, other = inquire(adopter, live), inquire(waiting, live)
    assert post(poster, chosen, "screen").status_code == 200
    assert post(poster, chosen, "reserve").status_code == 200
    _hide(live)
    chosen.refresh_from_db(); other.refresh_from_db(); live.refresh_from_db()
    assert live.status == ListingStatus.PENDING and chosen.status == InquiryStatus.ACTIVE
    assert chosen.reserved_at is not None
    assert other.status == InquiryStatus.WITHDRAWN
    assert post(poster, chosen, "complete").status_code == 200      # the adoption isn't reversed
    live.refresh_from_db()
    assert live.status == ListingStatus.ADOPTED and live.hidden_at is not None


@pytest.mark.django_db
def test_screening_and_reserving_are_refused_on_a_hidden_listing():
    poster, adopter = person(), person(member=True)
    live = listing(poster)
    inq = inquire(adopter, live)
    assert post(poster, inq, "screen").status_code == 200
    _hide(live)
    # the inquiry was closed by the takedown, so it's closed before it's anything else
    assert post(poster, inq, "reserve").status_code == 409


@pytest.mark.django_db
def test_hiding_twice_is_a_no_op_and_restore_clears_only_hidden_at():
    poster, a = person(), person()
    live = listing(poster)
    inq = inquire(a, live)
    _hide(live)
    first = live.hidden_at
    _hide(live)
    assert live.hidden_at == first
    restore_listing(AdoptionListing.objects.get(pk=live.pk), _staff(), "wrong call")
    live.refresh_from_db(); inq.refresh_from_db()
    assert live.hidden_at is None and live.status == ListingStatus.AVAILABLE
    assert inq.status == InquiryStatus.WITHDRAWN        # a restore doesn't reopen what it closed
    assert c(person()).get(f"/api/v1/listings/{live.pk}").status_code == 200
    with pytest.raises(ListingNotHidden):
        restore_listing(live, _staff(), "again")


@pytest.mark.django_db
def test_actioning_a_listing_flag_takes_it_down_and_dismissing_does_not():
    poster, reporter = person(), person()
    live, other = listing(poster), listing(poster, name="Mimi")
    staff = _staff()
    f1 = ModerationFlag.objects.create(reporter_account=reporter, target_type=FlagTarget.LISTING,
                                       target_id=live.pk, reason="selling a pet")
    f2 = ModerationFlag.objects.create(reporter_account=reporter, target_type=FlagTarget.LISTING,
                                       target_id=other.pk, reason="meh")
    _, effects = resolve_flag(f1, staff, FlagStatus.ACTIONED)
    resolve_flag(f2, staff, FlagStatus.DISMISSED)
    live.refresh_from_db(); other.refresh_from_db()
    assert live.hidden_at is not None and effects == {"listing_hidden": str(live.pk)}
    assert other.hidden_at is None


@pytest.mark.django_db
def test_a_flag_on_a_listing_that_is_gone_is_still_resolvable():
    flag = ModerationFlag.objects.create(reporter_account=person(), target_type=FlagTarget.LISTING,
                                         target_id="00000000-0000-0000-0000-000000000001", reason="x")
    resolve_flag(flag, _staff(), FlagStatus.ACTIONED)

