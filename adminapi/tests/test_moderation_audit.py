"""Review step 14 (MO21) · the audit row for a moderation decision says what was decided about, not only which flag.

The audit row's own target is the first id in the path, which for a flag decision is the FLAG, so "who took down this listing?" needed a
join to a table with no key to the content."""
import json

import pytest

from accounts.factories import AccountFactory
from adminapi.models import AdminAuditLog
from adminapi.tests.conftest import full_signin
from listings.tests.adoption_helpers import listing, person
from moderation.models import ModerationFlag


def _flag(target_type="listing", target_id=None):
    return ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type=target_type,
                                         target_id=target_id or listing(person()).pk, reason="x")


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


@pytest.mark.django_db
def test_the_audit_row_names_the_thing_decided_about_the_outcome_and_the_category(client, auth):
    target = listing(person())
    flag = _flag("listing", target.pk)
    client.post(f"/admin-api/flags/{flag.flag_id}/action", {"reason_code": "spam_scam", "note": "internal"},
                content_type="application/json", **auth)
    row = AdminAuditLog.objects.filter(action="flags/{id}/action").first()
    assert row.target_id == flag.flag_id                                           # the path's id is still the flag
    assert row.detail["flagged_type"] == "listing" and row.detail["flagged_id"] == str(target.pk)
    assert row.detail["decision"] == "actioned" and row.detail["reason_code"] == "spam_scam" and row.detail["effects"] == ["listing_hidden"]
    assert "internal" not in json.dumps(row.detail)


@pytest.mark.django_db
def test_a_refused_decision_leaves_the_plain_audit_row_and_a_restore_names_what_it_restored(client, auth):
    flag = _flag("listing", AccountFactory().pk)
    client.post(f"/admin-api/flags/{flag.flag_id}/action", {"reason_code": "spam_scam"}, content_type="application/json", **auth)
    refused = AdminAuditLog.objects.filter(action="flags/{id}/action").first()
    assert refused.outcome == "error_422" and "flagged_type" not in (refused.detail or {})


@pytest.mark.django_db
def test_the_audit_keeps_only_allow_listed_extra_keys():
    from adminapi.audit import EXTRA_FIELDS, _detail

    class R:
        GET = {}
        _audit_body = None
        _audit_extra = {"flagged_type": "listing", "password": "hunter2", "note": "text", "decision": "dismissed"}
    assert _detail(R()) == {"flagged_type": "listing", "decision": "dismissed"}
    assert "password" not in EXTRA_FIELDS and "note" not in EXTRA_FIELDS
