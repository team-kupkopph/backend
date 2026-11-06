"""Review step 3 (MO3, V8, V9) · the server says what actioning a flag will do, before the click.

Actioning a listing used to close every open inquiry and tell each adopter, and end a rescuer's claim,
with no word to the moderator before or after. `target.on_action` is that word, and the test that
matters most here is the one that holds it to the truth: the counts it states must equal what the
takedown then actually changes."""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from community.models import StoryPost
from listings.models import AdoptionInquiry, EndReason, InquiryStatus
from listings.tests.adoption_helpers import inquire, listing, person
from moderation import targets
from moderation.actions import resolve_flag
from moderation.models import FlagTarget, ModerationFlag
from sagip.models import (
    MatchStatus,
    OfferStatus,
    OfferType,
    ReportMatch,
    ReportOffer,
    RescueCase,
    StrayReport,
    StrayStatus,
)


def _on_action(target_type, pk):
    return targets.describe_target(target_type, pk)["on_action"]


def _flag(target_type, pk):
    return ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type=target_type,
                                         target_id=pk, reason="x")


def _report(**kw):
    return StrayReport.objects.create(species="dog", condition="injured",
                                      geom=Point(121.05, 14.63, srid=4326), **kw)


def _offer(report, status=OfferStatus.OPEN):
    return ReportOffer.objects.create(report=report, account=AccountFactory(), offer_type=OfferType.values[0],
                                      status=status, expires_at=timezone.now() + timezone.timedelta(hours=48))


def _claimed_report():
    report = _report(status=StrayStatus.CLAIMED)
    RescueCase.objects.create(report=report, claimed_by_account=AccountFactory())
    return report


def _text(on_action):
    return " ".join(on_action["consequences"])


@pytest.mark.django_db
def test_a_listing_says_how_many_open_inquiries_it_will_close_and_that_the_reserved_one_stays():
    poster = person()
    live = listing(poster)
    for _ in range(2):
        inquire(person(), live)
    reserved = inquire(person(), live)
    AdoptionInquiry.objects.filter(pk=reserved.pk).update(reserved_at=timezone.now())

    out = _on_action(FlagTarget.LISTING, live.pk)

    assert out["label"] == "Remove listing" and out["changes"] is True and out["already_done"] is False
    text = _text(out)
    assert "2 open inquiries will be closed" in text and "adopter" in text
    assert "reserved" in text and "stays" in text
    assert "poster is told" in text
    assert "does not reopen" in text.lower()


@pytest.mark.django_db
def test_one_inquiry_reads_in_the_singular_and_none_leaves_the_line_out():
    one, none = listing(person()), listing(person())
    inquire(person(), one)
    assert "1 open inquiry will be closed" in _text(_on_action(FlagTarget.LISTING, one.pk))
    assert "inquir" not in _text(_on_action(FlagTarget.LISTING, none.pk)).replace("The reserved", "")


@pytest.mark.django_db
def test_an_already_hidden_listing_says_nothing_more_will_change():
    """MO4's visible edge: a second flag on a taken-down listing must not offer to take it down again."""
    from listings.moderation import hide_listing
    live = listing(person())
    hide_listing(live, AccountFactory())
    out = _on_action(FlagTarget.LISTING, live.pk)
    assert out["already_done"] is True and out["changes"] is False and out["label"] == "Mark handled"
    assert "Already hidden" in _text(out)


@pytest.mark.django_db
def test_the_listing_numbers_are_the_numbers_the_takedown_then_changes():
    """The honesty check. A warning that undercounts is worse than none."""
    live = listing(person())
    for _ in range(3):
        inquire(person(), live)
    said = _on_action(FlagTarget.LISTING, live.pk)
    resolve_flag(_flag("listing", live.pk), AccountFactory(), "actioned")
    closed = AdoptionInquiry.objects.filter(listing=live, status=InquiryStatus.WITHDRAWN,
                                            end_reason=EndReason.LISTING_WITHDRAWN).count()
    assert closed == 3 and "3 open inquiries will be closed" in _text(said)


@pytest.mark.django_db
def test_a_claimed_report_says_the_claim_ends_the_rescuer_is_told_and_counts_the_offers_and_matches():
    report = _claimed_report()
    _offer(report), _offer(report, OfferStatus.MATCHED)
    other = _report()
    ReportMatch.objects.create(report=report, matched_report=other, status=MatchStatus.SUGGESTED)

    out = _on_action(FlagTarget.REPORT, report.pk)

    assert out["label"] == "Remove report" and out["changes"] is True
    text = _text(out)
    assert "claim" in text and "rescuer" in text and "told" in text
    assert "2 offers will expire" in text
    assert "1 suggested match will be dismissed" in text
    assert "map" in text


@pytest.mark.django_db
def test_a_report_in_someones_care_says_the_animal_stays_with_them_and_does_not_end_a_claim():
    report = _report(status=StrayStatus.RESCUED)
    RescueCase.objects.create(report=report, claimed_by_account=AccountFactory())
    text = _text(_on_action(FlagTarget.REPORT, report.pk))
    assert "already in someone's care" in text and "stays with them" in text
    assert "claim will end" not in text


@pytest.mark.django_db
def test_the_report_numbers_are_the_numbers_the_takedown_then_changes():
    report = _claimed_report()
    _offer(report), _offer(report), _offer(report, OfferStatus.MATCHED)
    said = _text(_on_action(FlagTarget.REPORT, report.pk))
    resolve_flag(_flag("report", report.pk), AccountFactory(), "actioned")
    expired = ReportOffer.objects.filter(report=report, status=OfferStatus.EXPIRED).count()
    assert expired == 3 and "3 offers will expire" in said


@pytest.mark.django_db
def test_a_story_says_it_is_hidden_kept_and_that_the_author_is_not_told():
    """MO7 stated plainly until step 9 changes it: the moderator should know the author hears nothing."""
    story = StoryPost.objects.create(author_account=AccountFactory(), story_type="general", caption="x")
    out = _on_action(FlagTarget.STORY, story.pk)
    assert out["label"] == "Hide story" and out["changes"] is True
    text = _text(out)
    assert "hidden from the feed" in text and "kept" in text and "not told" in text


@pytest.mark.django_db
def test_an_account_flag_says_it_records_the_decision_only_and_never_suspends():
    """MQ2 · the one type left with no take-down: suspension is its own deliberate act. Shifts and QRs now
    take real action (test_real_actions.py)."""
    owner = AccountFactory(account_type="shelter")
    out = _on_action(FlagTarget.ACCOUNT, owner.pk)
    assert out["label"] == "Mark handled" and out["changes"] is False and out["already_done"] is False
    assert "records your decision only" in _text(out).lower()
    assert "does not suspend" in _text(out).lower()


@pytest.mark.django_db
def test_every_supported_type_has_a_consequences_source():
    """A new FlagTarget with a describer but no consequences fails here, not in front of a moderator."""
    for target_type, spec in targets.REGISTRY.items():
        if spec.supported:
            assert spec.consequences is not None, target_type


@pytest.mark.django_db
def test_a_consequences_function_that_raises_says_so_and_does_not_fail_the_page(monkeypatch, caplog):
    live = listing(person())

    def boom(obj):
        raise ValueError("odd row")
    monkeypatch.setattr(targets.REGISTRY[FlagTarget.LISTING], "consequences", boom)
    out = _on_action(FlagTarget.LISTING, live.pk)
    assert out["label"] == "Remove listing" and "couldn't work out" in _text(out)
    assert "odd row" in caplog.text
