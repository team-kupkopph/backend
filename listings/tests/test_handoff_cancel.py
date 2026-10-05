"""C14 / D11 · a rescuer can take back a handoff that hasn't happened yet, and an unanswered
placement doesn't hold the animal's handoff forever (it expires after 7 days; both sides hear)."""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from listings.models import AdoptionInquiry, AdoptionListing, InquiryStatus, ListingStatus
from listings.sweeps import PLACEMENT_EXPIRY_DAYS, expire_placements
from notifications.models import Notification
from sagip.models import RescueCase, StrayReport, StrayStatus
from verifications.models import AccountCapability


def _c(a):
    c = APIClient(); c.force_authenticate(user=a); return c


def _verified(a):
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


def _safe_case(rescuer):
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
        status=StrayStatus.SAFE, geom=Point(121.05, 14.63, srid=4326))
    return RescueCase.objects.create(report=report, claimed_by_account=rescuer)


def _place(rescuer, case, recipient):
    return _c(rescuer).post(f"/api/v1/cases/{case.pk}/place",
        {"recipient_email": recipient.email, "name": "Bruno", "adoption_fee": "0"}, format="json")


def _cancel(who, case, body=None):
    return _c(who).post(f"/api/v1/cases/{case.pk}/handoff/cancel", body, format="json")


@pytest.mark.django_db
def test_a_rescuer_can_take_back_an_unanswered_placement():
    rescuer, recipient = _verified(AccountFactory()), _verified(AccountFactory())
    case = _safe_case(rescuer)
    placed = _place(rescuer, case, recipient).json()
    res = _cancel(rescuer, case)
    assert res.status_code == 200
    inq = AdoptionInquiry.objects.get(pk=placed["inquiry_id"])
    assert inq.status == InquiryStatus.WITHDRAWN and inq.listing.status == ListingStatus.WITHDRAWN
    [w] = Notification.objects.filter(account=recipient, type="placement_withdrawn")
    assert w.title == "A placement offer was withdrawn"
    assert w.body == f"{rescuer.display_name} is no longer offering Bruno to you."
    assert _place(rescuer, case, _verified(AccountFactory())).status_code == 201   # free again


@pytest.mark.django_db
def test_a_draft_can_be_cancelled():
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    lid = _c(rescuer).post(f"/api/v1/cases/{case.pk}/list", {}, format="json").json()["listing_id"]
    assert AdoptionListing.objects.get(pk=lid).status == ListingStatus.DRAFT
    assert _cancel(rescuer, case).status_code == 200
    assert AdoptionListing.objects.get(pk=lid).status == ListingStatus.WITHDRAWN


@pytest.mark.django_db
def test_a_public_listing_with_active_inquiries_cannot_be_cancelled():
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    lid = _c(rescuer).post(f"/api/v1/cases/{case.pk}/list", {}, format="json").json()["listing_id"]
    AdoptionListing.objects.filter(pk=lid).update(status=ListingStatus.AVAILABLE)
    AdoptionInquiry.objects.create(listing_id=lid, adopter_account=AccountFactory(),
                                   status=InquiryStatus.ACTIVE)
    res = _cancel(rescuer, case)
    assert res.status_code == 409 and res.json()["error"]["code"] == "has_active_inquiries"
    assert res.json()["error"]["message"] == ("People have asked about this animal, so it can't be "
                                              "taken down from here.")
    assert res.json()["error"]["details"] == {"active_inquiries": 1}
    assert AdoptionListing.objects.get(pk=lid).status == ListingStatus.AVAILABLE   # nothing changed
    assert not Notification.objects.filter(type="listing_withdrawn").exists()


@pytest.mark.django_db
def test_only_the_claimer_cancels_and_only_something_live():
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    assert _cancel(AccountFactory(), case).status_code == 403
    assert _cancel(rescuer, case).json()["error"]["code"] == "no_handoff"


@pytest.mark.django_db
def test_an_unanswered_placement_expires_after_seven_days_and_both_sides_hear():
    rescuer, recipient = _verified(AccountFactory()), _verified(AccountFactory())
    case = _safe_case(rescuer)
    placed = _place(rescuer, case, recipient).json()
    later = timezone.now() + timezone.timedelta(days=PLACEMENT_EXPIRY_DAYS, minutes=1)
    assert [str(i.pk) for i in expire_placements(now=later)] == [placed["inquiry_id"]]
    assert expire_placements(now=later) == []                       # idempotent
    [n] = Notification.objects.filter(account=rescuer, type="placement_decided")
    assert n.data["decision"] == "expired"
    # The recipient is told it lapsed — not that the rescuer took it back.
    [w] = Notification.objects.filter(account=recipient, type="placement_withdrawn")
    assert w.title == "A placement offer expired"
    assert w.body == "The offer of Bruno expired after 7 days without an answer."


# ── A former claimer can't act on the next claimer's rescue ─────────────────────────────
def _released_then_reclaimed():
    """A claims and releases; B claims the same report, brings it to safe and places it. A's old
    case row survives (expired_at set) and still names A as its claimer."""
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
        status=StrayStatus.REPORTED, geom=Point(121.05, 14.63, srid=4326))
    a, b, recipient = (_verified(AccountFactory()) for _ in range(3))
    old = _c(a).post(f"/api/v1/reports/{report.pk}/claim").json()["case_id"]
    assert _c(a).post(f"/api/v1/cases/{old}/release", {"reason": "cant_get_there"},
                      format="json").status_code == 200
    new = _c(b).post(f"/api/v1/reports/{report.pk}/claim").json()["case_id"]
    for step in (StrayStatus.RESCUED, StrayStatus.SAFE):
        assert _c(b).post(f"/api/v1/cases/{new}/status", {"status": step},
                          format="json").status_code == 200
    placed = _place(b, RescueCase.objects.get(pk=new), recipient)
    assert placed.status_code == 201
    return a, RescueCase.objects.get(pk=old), placed.json(), recipient


def _assert_case_expired(res):
    assert res.status_code == 409
    err = res.json()["error"]
    assert (err["code"], err["message"]) == ("case_expired", "This claim has lapsed")


@pytest.mark.django_db
def test_a_former_claimer_cannot_cancel_the_next_claimers_placement():
    a, old_case, placed, _ = _released_then_reclaimed()
    _assert_case_expired(_cancel(a, old_case))
    inq = AdoptionInquiry.objects.get(pk=placed["inquiry_id"])
    assert inq.status == InquiryStatus.ACTIVE and inq.listing.status == ListingStatus.PENDING


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["list", "place"])
def test_a_former_claimer_cannot_list_or_place_from_a_lapsed_case(action):
    a, old_case, placed, _ = _released_then_reclaimed()
    # Withdrawn so the live-handoff guard can't be what refuses them.
    AdoptionListing.objects.filter(pk=placed["listing_id"]).update(status=ListingStatus.WITHDRAWN)
    if action == "list":
        res = _c(a).post(f"/api/v1/cases/{old_case.pk}/list", {}, format="json")
    else:
        res = _place(a, old_case, _verified(AccountFactory()))
    _assert_case_expired(res)
    assert AdoptionListing.objects.filter(source_report=old_case.report).count() == 1


def _decided_outcomes(caplog):
    import json
    return [e["outcome"] for e in (json.loads(r.getMessage()) for r in caplog.records
                                   if r.name == "kupkop.analytics")
            if e["event"] == "inquiry_decided"]


@pytest.mark.django_db
def test_a_cancelled_placement_and_an_expired_one_are_counted_as_decisions(caplog):
    import logging
    rescuer = _verified(AccountFactory())
    with caplog.at_level(logging.INFO, logger="kupkop.analytics"):
        cancelled = _safe_case(rescuer)
        _place(rescuer, cancelled, _verified(AccountFactory()))
        _cancel(rescuer, cancelled)
        assert _decided_outcomes(caplog) == ["withdrawn"]

        _place(rescuer, _safe_case(rescuer), _verified(AccountFactory()))
        expire_placements(now=timezone.now() + timezone.timedelta(days=PLACEMENT_EXPIRY_DAYS, minutes=1))
        assert _decided_outcomes(caplog) == ["withdrawn", "expired"]

        drafted = _safe_case(rescuer)                     # a draft has no inquiry to decide
        _c(rescuer).post(f"/api/v1/cases/{drafted.pk}/list", {}, format="json")
        _cancel(rescuer, drafted)
        assert _decided_outcomes(caplog) == ["withdrawn", "expired"]


# ── The cancel guards and the expiry sweep's skips ──────────────────────────────────────
@pytest.mark.django_db
def test_cancelling_an_unknown_case_is_404_not_found():
    import uuid
    res = _c(_verified(AccountFactory())).post(f"/api/v1/cases/{uuid.uuid4()}/handoff/cancel")
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_cancelling_when_the_animal_is_no_longer_safe_is_409_case_not_safe():
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    StrayReport.objects.filter(pk=case.report_id).update(status=StrayStatus.CLAIMED)
    res = _cancel(rescuer, case)
    assert res.status_code == 409 and res.json()["error"]["code"] == "case_not_safe"


@pytest.mark.django_db
def test_cancelling_an_already_adopted_listing_is_409_and_leaves_it_alone():
    """C14 · the report can still read SAFE while its listing is ADOPTED — the animal has a home,
    so there is nothing to take back."""
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    listing = AdoptionListing.objects.create(posted_by=rescuer, source_report=case.report,
        species="dog", name="Bruno", city="Marikina", adoption_fee="0", status=ListingStatus.ADOPTED)
    res = _cancel(rescuer, case)
    assert res.status_code == 409 and res.json()["error"]["code"] == "already_adopted"
    listing.refresh_from_db()
    assert listing.status == ListingStatus.ADOPTED


@pytest.mark.django_db
def test_a_placement_six_days_old_is_not_expired():
    rescuer, recipient = _verified(AccountFactory()), _verified(AccountFactory())
    case = _safe_case(rescuer)
    placed = _place(rescuer, case, recipient).json()
    assert expire_placements(now=timezone.now() + timezone.timedelta(days=6)) == []
    assert AdoptionInquiry.objects.get(pk=placed["inquiry_id"]).status == InquiryStatus.ACTIVE
    assert not Notification.objects.filter(type__in=["placement_decided", "placement_withdrawn"]).exists()


@pytest.mark.django_db
def test_the_sweep_skips_an_inquiry_that_was_decided_after_the_candidate_scan(monkeypatch):
    """C14 · the sweep reads its candidates, then locks each row; one answered in between (here:
    withdrawn while the first is being expired) is skipped, not expired twice or notified."""
    from listings import sweeps
    rescuer = _verified(AccountFactory())
    ids = []
    for _ in range(2):
        ids.append(_place(rescuer, _safe_case(rescuer), _verified(AccountFactory())).json()["inquiry_id"])
    real = sweeps.notices.placement_decided

    def decided_then_the_other_is_withdrawn(listing, inquiry, decision):
        real(listing, inquiry, decision)
        AdoptionInquiry.objects.filter(pk__in=ids).exclude(pk=inquiry.pk).update(
            status=InquiryStatus.WITHDRAWN)
    monkeypatch.setattr(sweeps.notices, "placement_decided", decided_then_the_other_is_withdrawn)

    expired = expire_placements(now=timezone.now() + timezone.timedelta(days=PLACEMENT_EXPIRY_DAYS, minutes=1))
    assert len(expired) == 1
    assert Notification.objects.filter(account=rescuer, type="placement_decided").count() == 1
    other = AdoptionInquiry.objects.exclude(pk=expired[0].pk).get(pk__in=ids)
    assert other.status == InquiryStatus.WITHDRAWN        # the racing decision stands


@pytest.mark.django_db
def test_the_sweep_leaves_an_old_public_inquiry_on_a_pending_listing_alone():
    """C14 · only a placement (every stage skipped) expires; an inquiry with no skipped ladder is
    not one, however old."""
    listing = AdoptionListing.objects.create(posted_by=AccountFactory(), species="dog", name="Bruno",
        city="Marikina", adoption_fee="0", status=ListingStatus.PENDING)
    inq = AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory(),
                                         status=InquiryStatus.ACTIVE)
    later = timezone.now() + timezone.timedelta(days=PLACEMENT_EXPIRY_DAYS + 3)
    assert expire_placements(now=later) == []
    inq.refresh_from_db(); listing.refresh_from_db()
    assert inq.status == InquiryStatus.ACTIVE and listing.status == ListingStatus.PENDING
    assert not Notification.objects.filter(type__in=["placement_decided", "placement_withdrawn"]).exists()


# ── D15 · Take back closes open inquiries, after a confirm ──────────────────────────────
def _public_listing(rescuer, case, adopters, name="Bruno"):
    """An AVAILABLE listing for `case` with one ACTIVE inquiry per adopter."""
    lid = _c(rescuer).post(f"/api/v1/cases/{case.pk}/list", {}, format="json").json()["listing_id"]
    AdoptionListing.objects.filter(pk=lid).update(status=ListingStatus.AVAILABLE, name=name)
    return lid, [AdoptionInquiry.objects.create(listing_id=lid, adopter_account=a,
                                                status=InquiryStatus.ACTIVE) for a in adopters]


@pytest.mark.django_db
def test_the_confirmed_take_back_closes_every_open_inquiry_and_tells_each_adopter(caplog):
    import logging
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    adopters = [AccountFactory(), AccountFactory()]
    lid, [a_inq, b_inq] = _public_listing(rescuer, case, adopters)
    declined = AdoptionInquiry.objects.create(listing_id=lid, adopter_account=AccountFactory(),
                                              status=InquiryStatus.DECLINED)
    with caplog.at_level(logging.INFO, logger="kupkop.analytics"):
        res = _cancel(rescuer, case, {"close_inquiries": True})
    assert res.status_code == 200 and res.json() == {"status": "withdrawn", "closed_inquiries": 2}
    assert AdoptionListing.objects.get(pk=lid).status == ListingStatus.WITHDRAWN
    for inq in (a_inq, b_inq):
        inq.refresh_from_db()
        assert inq.status == InquiryStatus.WITHDRAWN and inq.decided_at is not None
    declined.refresh_from_db()
    assert declined.status == InquiryStatus.DECLINED and declined.decided_at is None   # untouched
    for adopter, inq in zip(adopters, (a_inq, b_inq), strict=True):
        [n] = Notification.objects.filter(account=adopter, type="listing_withdrawn")
        assert n.title == "No longer available"
        assert n.body == "Bruno is no longer available for adoption."
        assert n.data == {"listing_id": lid, "inquiry_id": str(inq.pk)}
    assert Notification.objects.filter(type="listing_withdrawn").count() == 2   # not the declined one
    assert _decided_outcomes(caplog) == ["listing_withdrawn", "listing_withdrawn"]
    assert _place(rescuer, case, _verified(AccountFactory())).status_code == 201   # free again


@pytest.mark.django_db
def test_a_listing_with_no_name_reads_this_animal_in_the_adopters_notice():
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    adopter = AccountFactory()
    _public_listing(rescuer, case, [adopter], name="")
    assert _cancel(rescuer, case, {"close_inquiries": True}).status_code == 200
    [n] = Notification.objects.filter(account=adopter, type="listing_withdrawn")
    assert n.body == "This animal is no longer available for adoption."


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [False, "true", 1, "yes", None])
def test_only_a_real_boolean_true_confirms_the_take_back(flag):
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    lid, [inq] = _public_listing(rescuer, case, [AccountFactory()])
    res = _cancel(rescuer, case, {"close_inquiries": flag})
    assert res.status_code == 409 and res.json()["error"]["code"] == "has_active_inquiries"
    assert AdoptionListing.objects.get(pk=lid).status == ListingStatus.AVAILABLE
    inq.refresh_from_db()
    assert inq.status == InquiryStatus.ACTIVE
    assert not Notification.objects.filter(type="listing_withdrawn").exists()


@pytest.mark.django_db
def test_the_flag_changes_nothing_for_a_draft_or_a_placement():
    rescuer, recipient = _verified(AccountFactory()), _verified(AccountFactory())
    drafted = _safe_case(rescuer)
    lid = _c(rescuer).post(f"/api/v1/cases/{drafted.pk}/list", {}, format="json").json()["listing_id"]
    res = _cancel(rescuer, drafted, {"close_inquiries": True})
    assert res.status_code == 200 and res.json() == {"status": "withdrawn"}
    assert AdoptionListing.objects.get(pk=lid).status == ListingStatus.WITHDRAWN

    placed_case = _safe_case(rescuer)
    placed = _place(rescuer, placed_case, recipient).json()
    res = _cancel(rescuer, placed_case, {"close_inquiries": True})
    assert res.status_code == 200 and res.json() == {"status": "withdrawn"}
    assert Notification.objects.filter(account=recipient, type="placement_withdrawn").count() == 1
    assert not Notification.objects.filter(type="listing_withdrawn").exists()
    assert AdoptionInquiry.objects.get(pk=placed["inquiry_id"]).status == InquiryStatus.WITHDRAWN


@pytest.mark.django_db
@pytest.mark.parametrize("body", [[1], "true"])
def test_a_take_back_body_that_is_not_an_object_is_a_plain_refusal(body):
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    lid, [inq] = _public_listing(rescuer, case, [AccountFactory()])
    res = _cancel(rescuer, case, body)
    assert res.status_code == 409 and res.json()["error"]["code"] == "has_active_inquiries"
    assert res.json()["error"]["details"] == {"active_inquiries": 1}
    assert AdoptionListing.objects.get(pk=lid).status == ListingStatus.AVAILABLE
    inq.refresh_from_db()
    assert inq.status == InquiryStatus.ACTIVE
    assert not Notification.objects.filter(type="listing_withdrawn").exists()


@pytest.mark.django_db
def test_a_take_back_moves_updated_at_on_the_listing_and_every_closed_inquiry():
    """Final review #4 · update_fields skips auto_now unless `updated_at` is named, on the confirmed
    close, and on the plain (nobody-asked) take-back of a draft."""
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    lid, [inq] = _public_listing(rescuer, case, [AccountFactory()])
    old = timezone.now() - timezone.timedelta(days=3)
    AdoptionListing.objects.filter(pk=lid).update(updated_at=old)
    AdoptionInquiry.objects.filter(pk=inq.pk).update(updated_at=old)
    assert _cancel(rescuer, case, {"close_inquiries": True}).status_code == 200
    listing = AdoptionListing.objects.get(pk=lid)
    inq.refresh_from_db()
    assert listing.updated_at > old + timezone.timedelta(days=2)
    assert inq.updated_at > old + timezone.timedelta(days=2)

    drafted = _safe_case(rescuer)
    did = _c(rescuer).post(f"/api/v1/cases/{drafted.pk}/list", {}, format="json").json()["listing_id"]
    AdoptionListing.objects.filter(pk=did).update(updated_at=old)
    assert _cancel(rescuer, drafted).status_code == 200
    assert AdoptionListing.objects.get(pk=did).updated_at > old + timezone.timedelta(days=2)


@pytest.mark.django_db
def test_a_reserved_public_listing_is_not_mistaken_for_a_placement():
    """AD22 · a public listing reserved for one applicant is `pending` with one active inquiry —
    exactly what a placement looked like. Take-back must ask first (D15), not withdraw it as a
    placement."""
    rescuer = _verified(AccountFactory()); case = _safe_case(rescuer)
    lid = _c(rescuer).post(f"/api/v1/cases/{case.pk}/list", {}, format="json").json()["listing_id"]
    AdoptionListing.objects.filter(pk=lid).update(status=ListingStatus.PENDING)
    AdoptionInquiry.objects.create(listing_id=lid, adopter_account=AccountFactory(),
                                   status=InquiryStatus.ACTIVE, reserved_at=timezone.now())
    res = _cancel(rescuer, case)
    assert res.status_code == 409 and res.json()["error"]["code"] == "has_active_inquiries"
    assert not Notification.objects.filter(type="placement_withdrawn").exists()
