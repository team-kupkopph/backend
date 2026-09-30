"""D1 + D8 (dev/sagip-build-review.md) · the people on a rescue can reach each other, by consent.

Owner decisions 2026-09-30:
  D1 — a consented contact reveal after a claim, mirroring the volunteer contact gate: each
       party (reporter, claimer, helper) opts in on their own row; phone and email only.
  D8 — the claimer sees who reported by default; "Report anonymously" hides it.

Who sees whom (only while a claim is active or resolved — never before, never after it lapses):
  reporter        -> the claimer
  active claimer  -> the reporter (unless anonymous) and every matched helper
  matched helper  -> the claimer
A contact block appears only for someone who consented; otherwise the key is absent.
"""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from sagip.models import CaseStatusHistory, ReportOffer, RescueCase, StrayReport
from sagip.sweeps import expire_stalled_claims
from verifications.models import AccountCapability


def _c(account=None):
    c = APIClient()
    if account is not None:
        c.force_authenticate(user=account)
    return c


def _person(**kw):
    a = AccountFactory(**kw)
    a.phone = f"+6391{str(a.pk.int)[:8]}"
    a.phone_verified_at = timezone.now()
    a.save(update_fields=["phone", "phone_verified_at"])
    return a


def _verified():
    a = _person()
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


def _report(reporter=None, **kw):
    fields = dict(species="dog", condition="healthy", status="reported", city="Marikina",
                  geom=Point(121.10, 14.65, srid=4326))
    fields.update(kw)
    return StrayReport.objects.create(reporter_account=reporter or _person(), **fields)


def _offer(report, account=None, share=False, offer_type="transport", note="Free after 6pm"):
    account = account or _person()
    res = _c(account).post(f"/api/v1/reports/{report.pk}/offers",
                           {"offer_type": offer_type, "note": note,
                            "contact_share_consent": share}, format="json")
    assert res.status_code == 201, res.content
    return ReportOffer.objects.get(pk=res.json()["offer_id"]), account


def _claim(report, claimer=None):
    claimer = claimer or _verified()
    res = _c(claimer).post(f"/api/v1/reports/{report.pk}/claim")
    assert res.status_code == 201, res.content
    return RescueCase.objects.get(pk=res.json()["case_id"]), claimer


def _people(report, viewer):
    return _c(viewer).get(f"/api/v1/reports/{report.pk}").json().get("people")


def _by_role(people, role):
    return [p for p in people if p["role"] == role]


def _contact(a):
    return {"phone": a.phone, "email": a.email}


# ── consent is captured where each person acts ───────────────────────────────────────
@pytest.mark.django_db
def test_a_reporter_can_consent_when_filing():
    reporter = _person()
    res = _c(reporter).post("/api/v1/reports", {
        "species": "dog", "condition": "healthy", "lat": 14.65, "lng": 121.10, "city": "Marikina",
        "contact_share_consent": True}, format="json")
    report = StrayReport.objects.get(pk=res.json()["report_id"])
    assert report.contact_share_consent is True and report.contact_share_consent_at is not None


@pytest.mark.django_db
def test_an_anonymous_report_cannot_share_contact():
    res = _c(_person()).post("/api/v1/reports", {
        "species": "dog", "condition": "healthy", "lat": 14.65, "lng": 121.10,
        "is_anonymous": True, "contact_share_consent": True}, format="json")
    assert res.status_code in (400, 422)
    assert not StrayReport.objects.exists()


@pytest.mark.django_db
def test_an_offer_carries_its_note_and_consent():
    offer, _ = _offer(_report(), share=True, note="I have a car")
    assert offer.note == "I have a car"
    assert offer.contact_share_consent is True and offer.contact_share_consent_at is not None


# ── who sees whom ────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_no_one_is_shown_before_a_claim():
    reporter = _person()
    report = _report(reporter, contact_share_consent=True)
    _, helper = _offer(report, share=True)
    assert _people(report, reporter) is None
    assert _people(report, helper) is None


@pytest.mark.django_db
def test_the_claimer_sees_the_reporter_by_name_and_contact_only_by_consent():
    reporter = _person(display_name="Ana")
    report = _report(reporter)
    _, claimer = _claim(report)

    [entry] = _by_role(_people(report, claimer), "reporter")
    assert entry["display_name"] == "Ana" and "contact" not in entry

    _c(reporter).post(f"/api/v1/reports/{report.pk}/contact", {"share": True}, format="json")
    [entry] = _by_role(_people(report, claimer), "reporter")
    assert entry["contact"] == _contact(reporter)


@pytest.mark.django_db
def test_an_anonymous_reporter_stays_anonymous_to_the_claimer():
    """D8 · 'Report anonymously' now means something: the claimer is told the reporter chose
    it, and gets neither their name nor their contact."""
    report = _report(_person(display_name="Ana"), is_anonymous=True)
    _, claimer = _claim(report)
    [entry] = _by_role(_people(report, claimer), "reporter")
    assert entry == {"role": "reporter", "anonymous": True}


@pytest.mark.django_db
def test_the_reporter_sees_the_claimer_contact_once_the_claimer_shares_it():
    reporter = _person()
    report = _report(reporter)
    case, claimer = _claim(report)
    [entry] = _by_role(_people(report, reporter), "claimer")
    assert entry["display_name"] == claimer.display_name and "contact" not in entry

    res = _c(claimer).post(f"/api/v1/cases/{case.pk}/contact", {"share": True}, format="json")
    assert res.status_code == 200 and res.json() == {"contact_shared": True}
    [entry] = _by_role(_people(report, reporter), "claimer")
    assert entry["contact"] == _contact(claimer)
    assert _by_role(_people(report, reporter), "helper") == []    # reporter sees the claimer only


@pytest.mark.django_db
def test_the_claimer_sees_every_matched_helper_with_what_they_offered():
    report = _report()
    _, sharing = _offer(report, _person(display_name="Ben"), share=True,
                        offer_type="transport", note="I have a car")
    _, private = _offer(report, _person(display_name="Cora"), share=False,
                        offer_type="supplies", note="")
    _, claimer = _claim(report)

    helpers = {h["display_name"]: h for h in _by_role(_people(report, claimer), "helper")}
    assert helpers[sharing.display_name]["offer_type"] == "transport"
    assert helpers[sharing.display_name]["note"] == "I have a car"
    assert helpers[sharing.display_name]["contact"] == _contact(sharing)
    assert "contact" not in helpers[private.display_name]


@pytest.mark.django_db
def test_a_matched_helper_sees_the_claimer_and_no_one_else():
    report = _report(_person(), contact_share_consent=True)
    _, helper = _offer(report)
    case, claimer = _claim(report)
    _c(claimer).post(f"/api/v1/cases/{case.pk}/contact", {"share": True}, format="json")

    people = _people(report, helper)
    assert [p["role"] for p in people] == ["claimer"]
    assert people[0]["contact"] == _contact(claimer)


@pytest.mark.django_db
def test_a_stranger_sees_no_one():
    report = _report(_person(), contact_share_consent=True)
    _claim(report)
    assert _people(report, _person()) is None
    assert _people(report, None) is None


@pytest.mark.django_db
def test_a_lapsed_claim_shows_no_one_to_anyone():
    reporter = _person()
    report = _report(reporter, contact_share_consent=True)
    case, claimer = _claim(report)
    CaseStatusHistory.objects.filter(report=report).update(
        changed_at=timezone.now() - timezone.timedelta(hours=30))
    expire_stalled_claims()
    assert _people(report, reporter) is None
    assert _people(report, claimer) is None


@pytest.mark.django_db
def test_an_unverified_phone_is_never_shared():
    reporter = _person()
    reporter.phone_verified_at = None
    reporter.save(update_fields=["phone_verified_at"])
    report = _report(reporter, contact_share_consent=True)
    _, claimer = _claim(report)
    [entry] = _by_role(_people(report, claimer), "reporter")
    assert entry["contact"] == {"phone": None, "email": reporter.email}


# ── each person controls their own consent ───────────────────────────────────────────
@pytest.mark.django_db
def test_each_person_sees_and_can_change_their_own_consent():
    reporter = _person()
    report = _report(reporter)
    offer, helper = _offer(report)
    case, claimer = _claim(report)

    assert _c(reporter).get(f"/api/v1/reports/{report.pk}").json()["contact_shared"] is False
    assert _c(claimer).get(f"/api/v1/reports/{report.pk}").json()["my_case"]["contact_shared"] is False
    [mine] = _c(helper).get(f"/api/v1/reports/{report.pk}").json()["my_offers"]
    assert mine == {"offer_id": str(offer.pk), "offer_type": "transport", "status": "matched",
                    "contact_shared": False}

    res = _c(helper).post(f"/api/v1/reports/{report.pk}/offers/{offer.pk}/contact",
                          {"share": True}, format="json")
    assert res.json() == {"contact_shared": True}
    _c(reporter).post(f"/api/v1/reports/{report.pk}/contact", {"share": True}, format="json")
    _c(reporter).post(f"/api/v1/reports/{report.pk}/contact", {"share": False}, format="json")
    report.refresh_from_db(); offer.refresh_from_db()
    assert report.contact_share_consent is False and report.contact_share_consent_at is None
    assert offer.contact_share_consent is True


@pytest.mark.django_db
def test_only_the_owner_can_change_a_consent():
    reporter = _person()
    report = _report(reporter)
    offer, _ = _offer(report)
    case, _ = _claim(report)
    other = _person()
    assert _c(other).post(f"/api/v1/reports/{report.pk}/contact", {"share": True},
                          format="json").status_code == 403
    assert _c(other).post(f"/api/v1/cases/{case.pk}/contact", {"share": True},
                          format="json").status_code == 403
    assert _c(other).post(f"/api/v1/reports/{report.pk}/offers/{offer.pk}/contact",
                          {"share": True}, format="json").status_code == 403


@pytest.mark.django_db
def test_an_anonymous_reporter_cannot_turn_sharing_on_later():
    reporter = _person()
    report = _report(reporter, is_anonymous=True)
    res = _c(reporter).post(f"/api/v1/reports/{report.pk}/contact", {"share": True}, format="json")
    assert res.status_code == 409 and res.json()["error"]["code"] == "anonymous_report"


@pytest.mark.django_db
def test_a_lapsed_claimer_cannot_change_the_case_consent():
    report = _report()
    case, claimer = _claim(report)
    CaseStatusHistory.objects.filter(report=report).update(
        changed_at=timezone.now() - timezone.timedelta(hours=30))
    expire_stalled_claims()
    res = _c(claimer).post(f"/api/v1/cases/{case.pk}/contact", {"share": True}, format="json")
    assert res.status_code == 409 and res.json()["error"]["code"] == "case_expired"


# ── the person's own export carries their consents ───────────────────────────────────
@pytest.mark.django_db
def test_the_data_export_includes_each_sagip_consent():
    from accounts.export import build_export

    reporter = _verified()
    report = _report(reporter, contact_share_consent=True)
    _offer(_report(), account=reporter, share=True)
    _claim(_report(), claimer=reporter)

    data = build_export(reporter)
    assert data["reports"][0]["contact_shared"] is True
    assert data["rescue_offers"][0]["contact_shared"] is True
    assert data["rescue_claims"][0]["contact_shared"] is False
    assert report
