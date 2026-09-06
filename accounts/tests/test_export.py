"""US-N3 · GET /me/export — RA 10173 portability (§12.6, §12.7).

D-S7-2: one synchronous, authenticated, throttled JSON response. No email delivery and no
async job — there is no Celery (US-F0 chose cron/commands over a broker), and standing up a
job queue for portability in the launch sprint is the wrong trade. The regulation asks for
machine-readable and timely, not asynchronous.

⚠️ **The export is the single widest authenticated read in the app**, which makes it the
easiest place to leak someone else. Two invariants carry that weight, and both are asserted
below rather than assumed:

  1. It contains only rows this account authored or owns — a shared row (an inquiry, a
     pledge) exports the caller's side, never the counterparty's contact details.
  2. §12.5's coarsening is NOT bypassed just because the caller is exporting. Precise
     coordinates appear only for reports the caller filed themselves.
"""
import json

import pytest
from django.contrib.gis.geos import Point

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for


def _auth(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


def _export(client, account):
    return client.get("/api/v1/me/export", **_auth(account))


def _report(reporter=None, **kw):
    from sagip.models import StrayReport
    defaults = dict(species="dog", condition="injured", geom=Point(121.05, 14.63, srid=4326),
                    reporter_account=reporter)
    defaults.update(kw)
    return StrayReport.objects.create(**defaults)


@pytest.mark.django_db
def test_export_requires_authentication(client):
    assert client.get("/api/v1/me/export").status_code == 401


@pytest.mark.django_db
def test_export_is_a_json_attachment_named_for_the_day(client):
    account = AccountFactory()
    res = _export(client, account)

    assert res.status_code == 200
    assert res["Content-Type"].startswith("application/json")
    # The mobile client hands this straight to the OS share sheet, so the filename is part
    # of the contract — a share sheet with no filename is a file nobody can find again.
    assert "attachment" in res["Content-Disposition"]
    assert "kupkop-export-" in res["Content-Disposition"]


@pytest.mark.django_db
def test_export_carries_the_account_and_its_settings(client):
    account = AccountFactory(display_name="Ana Reyes")
    body = _export(client, account).json()

    assert body["account"]["display_name"] == "Ana Reyes"
    assert body["account"]["email"] == account.email
    assert body["settings"]["analytics_consent"] is False
    assert "exported_at" in body


@pytest.mark.django_db
def test_password_hash_is_never_exported(client):
    # Portability is not a credential dump. The hash is not the user's data in any sense
    # they benefit from, and exporting it hands an attacker an offline cracking target.
    account = AccountFactory()
    account.set_password("correct-horse")
    account.save()

    assert "password_hash" not in json.dumps(_export(client, account).json())


@pytest.mark.django_db
def test_export_carries_the_users_own_reports(client):
    account = AccountFactory()
    _report(reporter=account, notes="found near the creek")

    reports = _export(client, account).json()["reports"]

    assert len(reports) == 1
    assert reports[0]["notes"] == "found near the creek"
    # Their own report, so §12.5's coarsening does not apply — this is the one caller
    # entitled to the precise pin they themselves dropped.
    assert reports[0]["lat"] == pytest.approx(14.63, abs=1e-4)


@pytest.mark.django_db
def test_someone_elses_report_is_not_in_my_export(client):
    account, stranger = AccountFactory(), AccountFactory()
    _report(reporter=stranger, notes="not mine")

    assert _export(client, account).json()["reports"] == []


@pytest.mark.django_db
def test_a_shared_inquiry_exports_my_side_without_the_other_partys_contact_details(client):
    """The sharpest leak risk in the whole endpoint: an inquiry is a row two people are on."""
    from listings.models import AdoptionInquiry, AdoptionListing, InquiryStatus
    adopter = AccountFactory()
    poster = AccountFactory(email="poster-secret@kupkop.ph", phone="+639170000001")
    listing = AdoptionListing.objects.create(posted_by=poster, name="Luna", species="dog",
                                             city="Marikina", adoption_fee="300.00")
    AdoptionInquiry.objects.create(listing=listing, adopter_account=adopter,
                                   message="Is she good with kids?",
                                   status=InquiryStatus.ACTIVE)

    raw = json.dumps(_export(client, adopter).json())

    assert "Is she good with kids?" in raw        # my side of it, which IS my data
    assert "Luna" in raw                          # the animal, which is public anyway
    assert "poster-secret@kupkop.ph" not in raw   # not mine to take
    assert "+639170000001" not in raw


@pytest.mark.django_db
def test_export_carries_my_pledges_stories_and_badges(client):
    from community.models import AccountBadge, Badge, NeedPledge, ShelterNeed, StoryPost
    account = AccountFactory()
    shelter = AccountFactory()
    need = ShelterNeed.objects.create(shelter_account=shelter, title="Dog food",
                                      category="food", quantity_needed=10)
    NeedPledge.objects.create(need=need, pledger_account=account, quantity=2)
    StoryPost.objects.create(author_account=account, story_type="general", caption="hello")
    badge = Badge.objects.filter(pk="first_shift").first()
    if badge:
        AccountBadge.objects.create(account=account, badge=badge)

    body = _export(client, account).json()

    assert len(body["pledges"]) == 1
    assert body["stories"][0]["caption"] == "hello"
    assert "badges" in body


@pytest.mark.django_db
def test_notifications_export_with_their_read_state(client):
    # Regression: this section was only ever exercised EMPTY, so it referenced a field the
    # model does not have (`read_at`; the column is a boolean `read`) and would have raised
    # on any account that had ever been notified — i.e. almost every real user.
    from notifications.models import Notification
    account = AccountFactory()
    Notification.objects.create(account=account, type="badge_earned",
                                title="New badge", body="First Kawang-Gawa", read=True)

    notifications = _export(client, account).json()["notifications"]

    assert len(notifications) == 1
    assert notifications[0]["type"] == "badge_earned"
    assert notifications[0]["read"] is True


@pytest.mark.django_db
def test_an_empty_account_still_exports_every_section(client):
    # A new user's export must not be a confusing half-document — the shape is the promise,
    # and an absent key reads as "we lost it" rather than "you have none".
    account = AccountFactory()
    body = _export(client, account).json()

    for section in ("account", "settings", "addresses", "pets", "reports", "listings",
                    "inquiries", "volunteer_signups", "stories", "pledges", "badges",
                    "notifications"):
        assert section in body, f"missing section: {section}"


@pytest.mark.django_db
def test_export_is_throttled(client, settings):
    # The heaviest authenticated read in the app, and a free amplification primitive
    # otherwise. Rate is pinned low deliberately.
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK}
    account = AccountFactory()
    codes = [_export(client, account).status_code for _ in range(5)]

    assert 429 in codes, f"expected a 429 within 5 calls, got {codes}"
