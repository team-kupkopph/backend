"""US-M1 · "Report this" on a stray report or listing."""
import pytest

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from moderation.models import FlagStatus, ModerationFlag


def _listing_id():
    from listings.models import AdoptionListing
    return str(AdoptionListing.objects.create(posted_by=AccountFactory(account_type="shelter"), name="Pogi", species="dog", city="Marikina City").pk)


def _report_id():
    from django.contrib.gis.geos import Point

    from sagip.models import StrayReport
    return str(StrayReport.objects.create(species="dog", condition="injured", geom=Point(121.05, 14.63, srid=4326)).pk)


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


@pytest.mark.django_db
def test_creates_a_flag(client):
    acc = AccountFactory()
    target = AccountFactory()
    res = client.post("/api/v1/moderation/flags",
                      {"target_type": "account", "target_id": str(target.pk),
                       "reason": "Spam messages"}, **_hdr(acc))
    assert res.status_code == 201
    flag = ModerationFlag.objects.get(pk=res.json()["flag_id"])
    assert flag.reporter_account_id == acc.pk
    assert flag.target_type == "account"
    assert str(flag.target_id) == str(target.pk)
    assert flag.status == FlagStatus.OPEN


@pytest.mark.django_db
def test_guest_is_refused(client):
    res = client.post("/api/v1/moderation/flags",
                      {"target_type": "listing", "target_id": "00000000-0000-0000-0000-000000000000",
                       "reason": "x"})
    assert res.status_code == 401


@pytest.mark.django_db
def test_a_second_open_flag_on_the_same_target_by_the_same_account_collapses(client):
    acc = AccountFactory()
    target_id = _listing_id()  # AT28: the target must exist
    first = client.post("/api/v1/moderation/flags",
                        {"target_type": "listing", "target_id": target_id, "reason": "Fake listing"},
                        **_hdr(acc))
    second = client.post("/api/v1/moderation/flags",
                         {"target_type": "listing", "target_id": target_id, "reason": "Still fake"},
                         **_hdr(acc))
    assert second.status_code == 200
    assert second.json()["flag_id"] == first.json()["flag_id"]
    assert ModerationFlag.objects.filter(target_id=target_id).count() == 1


@pytest.mark.django_db
def test_a_different_account_flagging_the_same_target_is_a_separate_flag(client):
    target_id = _report_id()
    a, b = AccountFactory(), AccountFactory()
    res_a = client.post("/api/v1/moderation/flags",
                        {"target_type": "report", "target_id": target_id, "reason": "x"}, **_hdr(a))
    res_b = client.post("/api/v1/moderation/flags",
                        {"target_type": "report", "target_id": target_id, "reason": "y"}, **_hdr(b))
    assert res_a.status_code == 201 and res_b.status_code == 201
    assert res_a.json()["flag_id"] != res_b.json()["flag_id"]


@pytest.mark.django_db
def test_flagging_again_after_the_earlier_flag_was_resolved_opens_a_fresh_one(client):
    acc = AccountFactory()
    target_id = _report_id()
    first = client.post("/api/v1/moderation/flags",
                        {"target_type": "report", "target_id": target_id, "reason": "first"},
                        **_hdr(acc))
    ModerationFlag.objects.filter(pk=first.json()["flag_id"]).update(status=FlagStatus.DISMISSED)

    second = client.post("/api/v1/moderation/flags",
                         {"target_type": "report", "target_id": target_id, "reason": "again"},
                         **_hdr(acc))
    assert second.status_code == 201
    assert second.json()["flag_id"] != first.json()["flag_id"]
    assert ModerationFlag.objects.filter(target_id=target_id).count() == 2


@pytest.mark.django_db
def test_an_unknown_target_type_is_refused(client):
    acc = AccountFactory()
    res = client.post("/api/v1/moderation/flags",
                      {"target_type": "carrier_pigeon",
                       "target_id": "00000000-0000-0000-0000-000000000000", "reason": "x"},
                      **_hdr(acc))
    assert res.status_code == 400


# AT28 (Abot-tulong QR plan, Run 1) · the flag's target was never checked: a random uuid (or one of the wrong type) was accepted, and flags on
# nothing piled up for moderators. The target must exist as the type it claims.
def _real_targets(owner):
    from django.contrib.gis.geos import Point
    from django.utils import timezone

    from listings.models import AdoptionListing
    from sagip.models import StrayReport
    from shelter.models import DonationQr
    from volunteer.models import VolunteerShift
    listing = AdoptionListing.objects.create(posted_by=owner, name="Pogi", species="dog", city="Marikina City")
    report = StrayReport.objects.create(species="dog", condition="injured", geom=Point(121.05, 14.63, srid=4326), reporter_account=owner)
    qr = DonationQr.objects.create(account=owner, provider="gcash", account_name="X", qr_image_url="u")
    shift = VolunteerShift.objects.create(shelter_account=owner, type="walking", capacity=2, status="open",
                                          starts_at=timezone.now() + timezone.timedelta(days=2),
                                          ends_at=timezone.now() + timezone.timedelta(days=2, hours=2))
    return {"account": owner.pk, "listing": listing.pk, "report": report.pk, "qr": qr.pk, "shift": shift.pk}


@pytest.mark.django_db
@pytest.mark.parametrize("target_type", ["account", "listing", "report", "qr", "shift"])
def test_flagging_a_target_that_does_not_exist_is_404_and_creates_nothing(client, target_type):
    acc = AccountFactory()
    res = client.post("/api/v1/moderation/flags",
                      {"target_type": target_type, "target_id": "11111111-1111-1111-1111-111111111111", "reason": "x"}, **_hdr(acc))
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"
    assert ModerationFlag.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("target_type", ["account", "listing", "report", "qr", "shift"])
def test_flagging_a_real_target_still_works(client, target_type):
    owner, reporter = AccountFactory(account_type="shelter"), AccountFactory()
    res = client.post("/api/v1/moderation/flags",
                      {"target_type": target_type, "target_id": str(_real_targets(owner)[target_type]), "reason": "x"}, **_hdr(reporter))
    assert res.status_code == 201


@pytest.mark.django_db
def test_a_real_id_of_the_wrong_type_is_404(client):
    owner, reporter = AccountFactory(account_type="shelter"), AccountFactory()
    targets = _real_targets(owner)
    res = client.post("/api/v1/moderation/flags",
                      {"target_type": "listing", "target_id": str(targets["qr"]), "reason": "x"}, **_hdr(reporter))
    assert res.status_code == 404
