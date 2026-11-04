"""SH14a–d, SH9 (shelter home review, 2026-10-29) · step 9, PATCH /shelter/profile hygiene.

- SH14b · the lock check and the save are one transaction on a locked row, and the save writes only what changed,
  so two devices editing different fields no longer lose one edit.
- SH14c · a replaced or cleared logo's object is discarded (the `shelter_logo` bucket is public).
- SH14d · `website_url` is an http(s) URL of at most 500 characters.
- SH14a / SQ3 (decided 2026-11-04: yes, key fields) · once the org is approved, a change to `official_email`,
  `official_phone` or `website_url`, and any attempt to rename it, leaves a `ShelterProfileChange` row a reviewer
  sees on the shelter's admin page (adminapi/tests/test_shelters.py).
"""
import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.export import build_export
from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from shelter.models import ShelterProfile, ShelterProfileChange
from verifications.models import VerificationRequest

URL = "/api/v1/shelter/profile"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter(*, verified=False, **profile):
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    defaults = dict(org_name="Happy Paws", org_type="shelter", tier="community_rescue",
                    official_email="old@paws.org", official_phone="+639170000001",
                    website_url="https://paws.org")
    defaults.update(profile)
    ShelterProfile.objects.create(account=acc, **defaults)
    if verified:
        VerificationRequest.objects.create(account=acc, type="shelter_org", status="approved")
    return acc


def _patch(client, acc, body):
    return client.patch(URL, body, content_type="application/json", **_hdr(acc))


def _logo(acc):
    return f"https://bkt.example/shelter_logo/{acc.pk}/{uuid.uuid4()}"


@pytest.mark.django_db
class TestSaveShape:
    def test_the_save_writes_only_the_changed_columns_on_a_locked_row(self, client):
        acc = _shelter()
        with CaptureQueriesContext(connection) as ctx:
            assert _patch(client, acc, {"org_name": "Happy Paws", "description": "New"}).status_code == 200
        sql = [q["sql"] for q in ctx.captured_queries]
        assert any('FROM "shelter_profile"' in q and "FOR UPDATE" in q for q in sql)
        [update] = [q for q in sql if q.startswith('UPDATE "shelter_profile"')]
        assert '"description"' in update
        assert '"org_name"' not in update and '"official_email"' not in update

    def test_an_edit_on_another_device_in_between_is_kept(self, client):
        # The second device's PATCH carries only its own field, so the first device's write survives.
        acc = _shelter()
        _patch(client, acc, {"description": "From the phone"})
        _patch(client, acc, {"operating_hours": "Sat 9-12"})
        p = ShelterProfile.objects.get(account=acc)
        assert (p.description, p.operating_hours) == ("From the phone", "Sat 9-12")


@pytest.mark.django_db
class TestWebsite:
    @pytest.mark.parametrize("url", ["https://paws.ph", "http://paws.ph/about?x=1", ""])
    def test_accepted(self, client, url):
        assert _patch(client, _shelter(), {"website_url": url}).status_code == 200

    def test_a_bare_address_is_read_as_https(self, client):
        acc = _shelter()
        assert _patch(client, acc, {"website_url": "facebook.com/pawsph"}).status_code == 200
        assert ShelterProfile.objects.get(account=acc).website_url == "https://facebook.com/pawsph"

    @pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://paws.ph", "paws dot ph",
                                     "https://paws.ph/" + "a" * 500])
    def test_refused(self, client, url):
        r = _patch(client, _shelter(), {"website_url": url})
        assert r.status_code == 400
        assert "website_url" in r.content.decode()


@pytest.mark.django_db
class TestLogoDiscard:
    @pytest.fixture
    def deleted(self, monkeypatch):
        calls = []
        monkeypatch.setattr("shelter.views.delete_object", lambda url, vis: calls.append((url, vis)))
        return calls

    def test_replacing_the_logo_discards_the_old_object_on_commit(self, client, deleted,
                                                                 django_capture_on_commit_callbacks):
        acc = _shelter()
        old = _logo(acc)
        ShelterProfile.objects.filter(account=acc).update(logo_url=old)
        with django_capture_on_commit_callbacks(execute=True):
            assert _patch(client, acc, {"logo_file_url": _logo(acc)}).status_code == 200
        assert deleted == [(old, "public")]

    def test_clearing_the_logo_discards_it(self, client, deleted, django_capture_on_commit_callbacks):
        acc = _shelter()
        old = _logo(acc)
        ShelterProfile.objects.filter(account=acc).update(logo_url=old)
        with django_capture_on_commit_callbacks(execute=True):
            _patch(client, acc, {"logo_file_url": ""})
        assert deleted == [(old, "public")]

    def test_resending_the_same_logo_discards_nothing(self, client, deleted, django_capture_on_commit_callbacks):
        acc = _shelter()
        same = _logo(acc)
        ShelterProfile.objects.filter(account=acc).update(logo_url=same)
        with django_capture_on_commit_callbacks(execute=True):
            _patch(client, acc, {"logo_file_url": same})
        assert deleted == []

    def test_a_legacy_url_that_is_not_this_accounts_upload_is_never_deleted(
            self, client, deleted, django_capture_on_commit_callbacks):
        acc = _shelter(logo_url="https://elsewhere.example/logo.png")
        with django_capture_on_commit_callbacks(execute=True):
            _patch(client, acc, {"logo_file_url": _logo(acc)})
        assert deleted == []


@pytest.mark.django_db
class TestAuditTrail:
    def test_a_verified_org_changing_a_key_field_leaves_a_row(self, client):
        acc = _shelter(verified=True)
        r = _patch(client, acc, {"official_email": "new@paws.org", "official_phone": "+639170000001",
                                 "description": "not audited"})
        assert r.status_code == 200
        rows = list(ShelterProfileChange.objects.filter(account=acc).values("field", "old_value", "new_value",
                                                                             "outcome"))
        assert rows == [{"field": "official_email", "old_value": "old@paws.org", "new_value": "new@paws.org",
                         "outcome": "changed"}]

    def test_a_refused_rename_of_a_verified_org_is_recorded_as_an_attempt(self, client):
        acc = _shelter(verified=True)
        assert _patch(client, acc, {"org_name": "Someone Else"}).status_code == 409
        row = ShelterProfileChange.objects.get(account=acc)
        assert (row.field, row.old_value, row.new_value, row.outcome) == (
            "org_name", "Happy Paws", "Someone Else", "refused")

    def test_before_approval_nothing_is_audited(self, client):
        acc = _shelter()
        _patch(client, acc, {"official_email": "new@paws.org", "org_name": "Renamed", "website_url": "https://x.ph"})
        assert not ShelterProfileChange.objects.filter(account=acc).exists()

    def test_the_shelter_export_carries_its_change_log(self, client):
        acc = _shelter(verified=True)
        _patch(client, acc, {"website_url": "https://new.paws.org"})
        doc = build_export(acc)
        assert [c["field"] for c in doc["shelter_profile_changes"]] == ["website_url"]
