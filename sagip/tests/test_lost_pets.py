"""D6 / S12 / S13 (dev/sagip-build-review.md) · lost pets.

Owner decision D6 (2026-09-30): a lost pet appears on the public map as its own kind of report,
"Lost pet · seen it?". Nobody can claim it (it's its owner's to find); "I've seen this pet"
files a FOUND report linked to it, which feeds the existing lost<->found matcher and tells the
owner. The exact last-seen spot stays with the owner (the public gets the same ~500 m grid
point as any report).

Before this the backend accepted `report_type=lost` but no client could send it (S12), and a lost
report would have been claimable, offerable and escalated to rescuers exactly like a stray (S13).
"""
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from listings.models import Pet, PetPhoto
from notifications.models import Notification
from sagip.models import MatchStatus, ReportMatch, StrayReport, StrayReportPhoto
from sagip.sweeps import escalate_reports
from verifications.models import AccountCapability

AT = {"lat": 14.6507, "lng": 121.1029, "city": "Marikina"}


def _c(account=None):
    c = APIClient()
    if account is not None:
        c.force_authenticate(user=account)
    return c


def _verified():
    a = AccountFactory()
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


def _pet(owner, **kw):
    fields = dict(name="Bruno", species="dog", breed="Aspin", color_markings="brown, white chest",
                  size_category="medium", sex="male")
    fields.update(kw)
    return Pet.objects.create(owner_account=owner, **fields)


def _file(who, **body):
    res = _c(who).post("/api/v1/reports", {**AT, **body}, format="json")
    assert res.status_code == 201, res.content
    return StrayReport.objects.get(pk=res.json()["report_id"])


def _lost(owner=None, **extra):
    owner = owner or AccountFactory()
    pet = _pet(owner)
    return _file(owner, report_type="lost", species="dog", pet_id=str(pet.pk), **extra), owner, pet


# ── filing a lost report ─────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_a_lost_report_needs_no_condition_and_takes_the_pets_details():
    report, _, _ = _lost()
    assert report.report_type == "lost" and report.condition == "healthy"
    assert (report.breed, report.color_markings, report.size_category, report.sex) == \
        ("Aspin", "brown, white chest", "medium", "male")


@pytest.mark.django_db
def test_a_lost_report_without_photos_uses_the_pets_photos_primary_first():
    owner = AccountFactory()
    pet = _pet(owner)
    PetPhoto.objects.create(pet=pet, url="s3://side.jpg", is_primary=False)
    PetPhoto.objects.create(pet=pet, url="s3://face.jpg", is_primary=True)
    report = _file(owner, report_type="lost", species="dog", pet_id=str(pet.pk))
    urls = list(StrayReportPhoto.objects.filter(report=report).order_by("uploaded_at")
                .values_list("url", flat=True))
    assert urls == ["s3://face.jpg", "s3://side.jpg"]


@pytest.mark.django_db
def test_a_stray_still_needs_its_condition():
    res = _c(AccountFactory()).post("/api/v1/reports", {**AT, "species": "dog"}, format="json")
    assert res.status_code in (400, 422)


# ── a lost pet is not a rescue job ───────────────────────────────────────────────────
@pytest.mark.django_db
def test_nobody_can_claim_or_offer_on_a_lost_pet():
    report, _, _ = _lost()
    claim = _c(_verified()).post(f"/api/v1/reports/{report.pk}/claim")
    assert claim.status_code == 409 and claim.json()["error"]["code"] == "not_claimable"
    offer = _c(AccountFactory()).post(f"/api/v1/reports/{report.pk}/offers",
                                      {"offer_type": "transport"}, format="json")
    assert offer.status_code == 409 and offer.json()["error"]["code"] == "not_claimable"


@pytest.mark.django_db
def test_a_found_animal_is_still_claimable():
    found = _file(AccountFactory(), report_type="found", species="dog", condition="healthy")
    assert _c(_verified()).post(f"/api/v1/reports/{found.pk}/claim").status_code == 201


@pytest.mark.django_db
def test_a_lost_pet_never_escalates_to_rescuers():
    from accounts.models import Address
    rescuer = _verified()
    Address.objects.create(account=rescuer, city="Marikina City", is_primary=True)
    report, _, _ = _lost()
    StrayReport.objects.filter(pk=report.pk).update(
        created_at=timezone.now() - timezone.timedelta(hours=30))
    assert escalate_reports() == []
    assert not Notification.objects.filter(type="report_escalated").exists()


# ── what the public sees ─────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_the_map_and_the_report_say_what_kind_of_report_it_is():
    report, _, _ = _lost()
    [row] = _c().get("/api/v1/reports/map?city=Marikina").json()["reports"]
    assert row["report_type"] == "lost"
    body = _c().get(f"/api/v1/reports/{report.pk}").json()
    assert body["report_type"] == "lost" and body["pet_name"] == "Bruno"
    assert body["describe"] == {"breed": "Aspin", "color_markings": "brown, white chest",
                                "size_category": "medium", "sex": "male"}
    assert "precise_location" not in body          # the exact last-seen spot stays with the owner


@pytest.mark.django_db
def test_a_stray_has_no_pet_name():
    stray = _file(AccountFactory(), species="dog", condition="injured")
    assert "pet_name" not in _c().get(f"/api/v1/reports/{stray.pk}").json()


# ── "I've seen this pet" ─────────────────────────────────────────────────────────────
def _sighting(lost, who=None, **extra):
    return _file(who or AccountFactory(), sighting_of=str(lost.pk), condition="healthy",
                 species="cat", **extra)       # species is taken from the lost report


@pytest.mark.django_db
def test_a_sighting_files_a_found_report_linked_to_the_lost_pet_and_tells_the_owner():
    lost, owner, _ = _lost()
    found = _sighting(lost)
    assert found.report_type == "found" and found.species == "dog"
    match = ReportMatch.objects.get(report=found, matched_report=lost)
    assert match.status == MatchStatus.SUGGESTED
    assert Notification.objects.filter(account=owner, type="match_suggested",
                                       data__report_id=str(lost.pk)).exists()
    listed = _c(owner).get(f"/api/v1/reports/{lost.pk}/matches").json()["results"]
    assert [m["report"]["report_id"] for m in listed] == [str(found.pk)]


@pytest.mark.django_db
def test_a_second_look_does_not_duplicate_the_match_or_the_push():
    lost, owner, _ = _lost()
    found = _sighting(lost)
    from sagip.matching import link_sighting
    link_sighting(found, lost)
    assert ReportMatch.objects.filter(report=found, matched_report=lost).count() == 1
    assert Notification.objects.filter(account=owner, type="match_suggested").count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["stray", "resolved", "own"])
def test_a_sighting_must_point_at_someone_elses_open_lost_report(target):
    lost, owner, _ = _lost()
    who = AccountFactory()
    if target == "stray":
        lost = _file(AccountFactory(), species="dog", condition="healthy")
    elif target == "resolved":
        StrayReport.objects.filter(pk=lost.pk).update(status="resolved")
    else:
        who = owner
    res = _c(who).post("/api/v1/reports", {**AT, "sighting_of": str(lost.pk), "species": "dog",
                                           "condition": "healthy"}, format="json")
    assert res.status_code == 422
    assert res.json()["error"]["code"] in ("not_a_lost_report", "own_report")


# ── reuniting: owner and finder can reach each other, by consent ─────────────────────
@pytest.mark.django_db
def test_the_owner_sees_the_finder_and_their_contact_only_if_they_consented():
    lost, owner, _ = _lost()
    finder = AccountFactory(display_name="Rosa")
    finder.phone, finder.phone_verified_at = "+639171112222", timezone.now()
    finder.save(update_fields=["phone", "phone_verified_at"])
    found = _sighting(lost, who=finder)

    [m] = _c(owner).get(f"/api/v1/reports/{lost.pk}/matches").json()["results"]
    assert m["reporter"] == {"display_name": "Rosa"}

    found.contact_share_consent = True
    found.save(update_fields=["contact_share_consent"])
    [m] = _c(owner).get(f"/api/v1/reports/{lost.pk}/matches").json()["results"]
    assert m["reporter"]["contact"] == {"phone": "+639171112222", "email": finder.email}


@pytest.mark.django_db
def test_an_anonymous_finder_stays_anonymous():
    lost, owner, _ = _lost()
    _sighting(lost, is_anonymous=True)
    [m] = _c(owner).get(f"/api/v1/reports/{lost.pk}/matches").json()["results"]
    assert m["reporter"] == {"anonymous": True}


# ── closing a lost report ────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_an_owner_can_close_a_lost_report_as_reunited():
    lost, owner, _ = _lost()
    res = _c(owner).post(f"/api/v1/reports/{lost.pk}/close", {"reason": "reunited"}, format="json")
    assert res.status_code == 200
    assert _c(owner).get(f"/api/v1/reports/{lost.pk}").json()["close_reason"] == "reunited"
