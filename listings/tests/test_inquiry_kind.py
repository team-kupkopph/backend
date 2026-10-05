"""AD22 · a placement is said outright (`kind`), not inferred from an all-skipped ladder."""
import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import AccountFactory
from listings.kinds import backfill_inquiry_kinds
from listings.models import (
    AdoptionInquiry,
    AdoptionListing,
    AdoptionStage,
    AdoptionStageKey,
    InquiryKind,
    StageState,
)


def _listing(**kw):
    return AdoptionListing.objects.create(posted_by=AccountFactory(), name="Rex", species="dog",
                                          city="Manila", **kw)


def _ladder(inquiry, state_for):
    for key in AdoptionStageKey:
        AdoptionStage.objects.create(inquiry=inquiry, stage_key=key, state=state_for(key))


@pytest.mark.django_db
def test_a_new_inquiry_is_an_inquiry_by_default():
    inq = AdoptionInquiry.objects.create(listing=_listing(), adopter_account=AccountFactory())
    assert inq.kind == InquiryKind.INQUIRY
    assert inq.accepted_at is None and inq.reserved_at is None
    assert inq.ended_by_account is None and inq.end_reason == ""


@pytest.mark.django_db
def test_the_backfill_marks_only_all_skipped_ladders_as_placements():
    listing = _listing(status="pending")
    placed = AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory())
    _ladder(placed, lambda key: StageState.SKIPPED)
    asked = AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory())
    _ladder(asked, lambda key: StageState.DONE if key == AdoptionStageKey.INQUIRY else StageState.SKIPPED)
    bare = AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory())

    assert backfill_inquiry_kinds(AdoptionInquiry, AdoptionStage) == 1
    kinds = dict(AdoptionInquiry.objects.values_list("pk", "kind"))
    assert kinds == {placed.pk: "placement", asked.pk: "inquiry", bare.pk: "inquiry"}
    assert backfill_inquiry_kinds(AdoptionInquiry, AdoptionStage) == 0      # idempotent


@pytest.mark.django_db
def test_a_listing_can_be_reserved_for_only_one_open_inquiry():
    listing = _listing(status="pending")
    now = timezone.now()
    AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory(), reserved_at=now)
    with pytest.raises(IntegrityError), transaction.atomic():
        AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory(), reserved_at=now)
    # a CLOSED inquiry that still carries a timestamp doesn't count
    AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory(),
                                   reserved_at=now, status="declined")
