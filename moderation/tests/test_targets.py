"""Review step 1 (MO2, MO6, MO17) · one registry says what each flaggable type is, how to read it, and
what a takedown does to it. Before it, `title` was read off a listing that has `name`, and the `getattr`
default turned the typo into a UUID that looked like content."""
import uuid

import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from community.models import StoryPhoto, StoryPost, StoryStatus
from listings.models import AdoptionListing, AdoptionListingPhoto
from listings.moderation import hide_listing
from moderation import targets
from moderation.actions import resolve_flag
from moderation.models import FlagTarget, ModerationFlag
from sagip.models import StrayReport, StrayReportPhoto
from sagip.moderation import hide_report
from shelter.models import DonationQr
from volunteer.models import VolunteerShift


def _flag(target_type, target_id):
    return ModerationFlag.objects.create(
        reporter_account=AccountFactory(), target_type=target_type, target_id=target_id, reason="x")


def _shift(owner, **kw):
    return VolunteerShift.objects.create(
        shelter_account=owner, type="walking", capacity=2, status="open",
        starts_at=timezone.now() + timezone.timedelta(days=2),
        ends_at=timezone.now() + timezone.timedelta(days=2, hours=2), **kw)


def _report(**kw):
    return StrayReport.objects.create(species="dog", condition="injured",
                                      geom=Point(121.05, 14.63, srid=4326), **kw)


def _listing(owner=None, **kw):
    kw.setdefault("name", "Bantay")
    return AdoptionListing.objects.create(posted_by=owner or AccountFactory(account_type="shelter"),
                                          species="dog", city="Marikina City", **kw)


def test_every_flag_target_has_a_spec():
    """A new enum value without a spec fails here, not in front of a moderator."""
    assert set(targets.REGISTRY) == set(FlagTarget.values)


def test_only_message_is_unsupported():
    assert [t for t, s in targets.REGISTRY.items() if not s.supported] == [FlagTarget.MESSAGE]


@pytest.mark.django_db
def test_a_listing_is_described_by_its_name_not_its_uuid():
    """P1 · MO2. The label used to be the listing's UUID."""
    listing = _listing(name="Free puppies", story="Take one now")
    out = targets.describe_target(FlagTarget.LISTING, listing.pk)
    assert out["state"] == "present"
    assert out["label"] == out["title"] == "Free puppies"
    assert out["body"] == "Take one now"
    assert str(listing.pk) not in out["label"]


@pytest.mark.django_db
def test_a_listing_names_its_poster_and_shows_its_photos():
    poster = AccountFactory(account_type="shelter", display_name="PAWS Manila")
    listing = _listing(poster)
    AdoptionListingPhoto.objects.create(listing=listing, url="https://x/1.jpg", is_primary=True)
    out = targets.describe_target(FlagTarget.LISTING, listing.pk)
    assert out["owner"] == {"account_id": str(poster.account_id), "display_name": "PAWS Manila"}
    assert out["photos"] == ["https://x/1.jpg"]
    assert out["hidden"] is False


@pytest.mark.django_db
@pytest.mark.parametrize("make", [
    lambda: (FlagTarget.LISTING, _listing(name="").pk),
    lambda: (FlagTarget.REPORT, _report(notes="").pk),
    lambda: (FlagTarget.STORY, StoryPost.objects.create(
        author_account=AccountFactory(), story_type="general", caption="").pk),
    lambda: (FlagTarget.SHIFT, _shift(AccountFactory(account_type="shelter"), title="").pk),
], ids=["listing", "report", "story", "shift"])
def test_blank_text_reads_as_blank_never_as_a_uuid(make):
    """U2 · the fallback names the gap in words; an id posing as content is what MO2 was."""
    target_type, pk = make()
    out = targets.describe_target(target_type, pk)
    assert out["state"] == "present"
    assert str(pk) not in out["label"]
    assert out["label"].strip()


@pytest.mark.django_db
def test_each_type_describes_something_real():
    owner = AccountFactory(account_type="shelter", display_name="Org")
    qr = DonationQr.objects.create(account=owner, provider="gcash", account_name="PAWS Inc",
                                   qr_image_url="https://x/qr.png")
    story = StoryPost.objects.create(author_account=owner, story_type="general", caption="Adopted!")
    StoryPhoto.objects.create(story=story, url="https://x/s.jpg", is_primary=True)
    report = _report(notes="limping near the market")
    StrayReportPhoto.objects.create(report=report, url="https://x/r.jpg")
    shift = _shift(owner, title="Morning dog walk", description="Bring water")
    seen = {
        FlagTarget.ACCOUNT: targets.describe_target(FlagTarget.ACCOUNT, owner.pk),
        FlagTarget.QR: targets.describe_target(FlagTarget.QR, qr.pk),
        FlagTarget.STORY: targets.describe_target(FlagTarget.STORY, story.pk),
        FlagTarget.REPORT: targets.describe_target(FlagTarget.REPORT, report.pk),
        FlagTarget.SHIFT: targets.describe_target(FlagTarget.SHIFT, shift.pk),
    }
    assert seen[FlagTarget.ACCOUNT]["label"] == "Org"
    assert "PAWS Inc" in seen[FlagTarget.QR]["label"] and seen[FlagTarget.QR]["photos"] == ["https://x/qr.png"]
    assert seen[FlagTarget.STORY]["label"] == "Adopted!" and seen[FlagTarget.STORY]["photos"] == ["https://x/s.jpg"]
    assert seen[FlagTarget.REPORT]["label"] == "limping near the market"
    assert seen[FlagTarget.REPORT]["photos"] == ["https://x/r.jpg"] and seen[FlagTarget.REPORT]["owner"] is None
    assert seen[FlagTarget.SHIFT]["label"] == "Morning dog walk" and seen[FlagTarget.SHIFT]["body"] == "Bring water"
    assert all(v["state"] == "present" for v in seen.values())


@pytest.mark.django_db
def test_a_missing_target_is_gone_and_message_is_unsupported():
    assert targets.describe_target(FlagTarget.LISTING, uuid.uuid4())["state"] == "gone"
    assert targets.describe_target(FlagTarget.MESSAGE, uuid.uuid4())["state"] == "unsupported"


@pytest.mark.django_db
def test_a_describer_that_raises_degrades_instead_of_failing_the_queue(monkeypatch, caplog):
    """U3 · a row with an odd shape must not 500 the detail page; it is logged so it gets fixed."""
    listing = _listing()

    def boom(obj):
        raise ValueError("bad row")
    monkeypatch.setattr(targets.REGISTRY[FlagTarget.LISTING], "describe", boom)
    out = targets.describe_target(FlagTarget.LISTING, listing.pk)
    assert out["state"] == "present" and out["label"].strip() and out["photos"] == []
    assert "bad row" in caplog.text


def test_a_database_error_is_not_reported_as_a_deleted_target(monkeypatch):
    """The old code turned any exception into 'Target no longer exists', which would tell a moderator
    to dismiss a flag because the database blinked."""
    from django.db import OperationalError

    def down(self, target_id):
        raise OperationalError("connection dropped")
    monkeypatch.setattr(targets.TargetSpec, "fetch", down)
    with pytest.raises(OperationalError):
        targets.describe_target(FlagTarget.LISTING, uuid.uuid4())


@pytest.mark.django_db
def test_a_second_takedown_of_a_hidden_listing_reports_no_side_effect():
    """P13 · MO17. side_effects said listing_hidden for a listing that was already hidden."""
    listing = _listing()
    reviewer = AccountFactory()
    _, first = resolve_flag(_flag("listing", listing.pk), reviewer, "actioned")
    _, second = resolve_flag(_flag("listing", listing.pk), reviewer, "actioned")
    assert first == {"listing_hidden": str(listing.pk)}
    assert second == {}


@pytest.mark.django_db
def test_a_second_takedown_of_a_hidden_report_or_story_reports_no_side_effect():
    reviewer = AccountFactory()
    report = _report()
    story = StoryPost.objects.create(author_account=AccountFactory(), story_type="general", caption="x")
    for target_type, pk, key in ((FlagTarget.REPORT, report.pk, "report_hidden"),
                                 (FlagTarget.STORY, story.pk, "story_hidden")):
        _, first = resolve_flag(_flag(target_type, pk), reviewer, "actioned")
        _, second = resolve_flag(_flag(target_type, pk), reviewer, "actioned")
        assert first == {key: str(pk)} and second == {}
    story.refresh_from_db()
    assert story.status == StoryStatus.HIDDEN


@pytest.mark.django_db
def test_an_account_flag_still_changes_nothing_on_the_account():
    """MQ2 · actioning an account flag records the judgement and never suspends: suspension is its own deliberate
    act. (Shifts and QRs used to be pinned here as changing nothing, MO5; review step 6 made them real, and
    moderation/tests/test_real_actions.py holds that.)"""
    owner = AccountFactory(account_type="shelter")
    flag, effects = resolve_flag(_flag(FlagTarget.ACCOUNT, owner.pk), AccountFactory(), "actioned")
    owner.refresh_from_db()
    assert effects == {} and flag.status == "actioned" and owner.status == "active"


@pytest.mark.django_db
def test_the_hide_functions_say_whether_they_hid_anything():
    reviewer = AccountFactory()
    listing = _listing()
    report = _report()
    assert hide_listing(listing, reviewer) is True
    assert hide_listing(listing, reviewer) is False
    assert hide_report(report, reviewer) is True
    assert hide_report(report, reviewer) is False
    assert hide_listing(AdoptionListing(listing_id=uuid.uuid4()), reviewer) is False
