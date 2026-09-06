"""US-C1/C2/C3 · the verification queue, detail and decisions."""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from adminapi.tests.conftest import full_signin
from verifications.models import (
    VerificationAccessLog,
    VerificationDocument,
    VerificationRequest,
)


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def make_request(*, status="pending", vtype="shelter_org", name="Alonzo Rescue", docs=()):
    account = AccountFactory(display_name=name, email=f"{name.replace(' ', '').lower()}@ex.com")
    vr = VerificationRequest.objects.create(account=account, type=vtype, status=status)
    for doc_type in docs:
        VerificationDocument.objects.create(verification=vr, doc_type=doc_type,
                                            file_url=f"s3://x/{doc_type}.jpg")
    return vr


# -- US-C1 · the queue -------------------------------------------------------------------
@pytest.mark.django_db
def test_the_queue_needs_a_staff_token(client):
    assert client.get("/admin-api/verifications").status_code in (401, 403)


@pytest.mark.django_db
def test_defaults_to_pending_oldest_first(client, auth):
    """A reviewer's queue is a BACKLOG. Newest-first starves the request that has waited
    longest — the one an applicant is actually blocked on."""
    old = make_request(name="Older Org")
    new = make_request(name="Newer Org")
    VerificationRequest.objects.filter(pk=old.pk).update(
        submitted_at=timezone.now() - timezone.timedelta(days=5))
    make_request(status="approved", name="Done Org")

    body = client.get("/admin-api/verifications", **auth).json()
    names = [r["applicant"]["display_name"] for r in body["results"]]
    assert names == ["Older Org", "Newer Org"], "approved must be excluded; oldest must lead"
    assert body["results"][0]["verification_id"] == str(old.verification_id)
    assert body["results"][1]["verification_id"] == str(new.verification_id)


@pytest.mark.django_db
def test_filters_by_status_and_type(client, auth):
    make_request(status="needs_info", name="Needs Info Org")
    make_request(vtype="rescuer", name="A Member")

    needs = client.get("/admin-api/verifications?status=needs_info", **auth).json()["results"]
    assert [r["applicant"]["display_name"] for r in needs] == ["Needs Info Org"]

    members = client.get("/admin-api/verifications?type=rescuer", **auth).json()["results"]
    assert [r["applicant"]["display_name"] for r in members] == ["A Member"]


@pytest.mark.django_db
def test_an_unknown_status_is_a_400_not_an_empty_list(client, auth):
    """An empty list would look like 'nothing to review' — the most dangerous possible answer
    to a typo'd filter on a backlog screen."""
    res = client.get("/admin-api/verifications?status=nonsense", **auth)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_status"


@pytest.mark.django_db
def test_search_matches_name_and_email(client, auth):
    make_request(name="Marikina Paws")
    make_request(name="Quezon Shelter")
    hits = client.get("/admin-api/verifications?q=marikina", **auth).json()["results"]
    assert [r["applicant"]["display_name"] for r in hits] == ["Marikina Paws"]
    by_email = client.get("/admin-api/verifications?q=quezonshelter@ex.com", **auth).json()["results"]
    assert [r["applicant"]["display_name"] for r in by_email] == ["Quezon Shelter"]


@pytest.mark.django_db
def test_pagination_yields_every_row_exactly_once(client, auth):
    """The property that matters on a queue: paging must not skip or repeat a request."""
    for i in range(30):
        make_request(name=f"Org {i:02d}")

    seen, cursor, pages = [], "", 0
    while True:
        body = client.get(f"/admin-api/verifications?cursor={cursor}", **auth).json()
        seen += [r["verification_id"] for r in body["results"]]
        pages += 1
        if not body["next_cursor"]:
            break
        cursor = body["next_cursor"]
        assert pages < 10, "pagination did not terminate"

    assert len(seen) == 30
    assert len(set(seen)) == 30, "a request was returned on two pages"


# -- US-C2 · detail, documents, checklist -------------------------------------------------
@pytest.mark.django_db
def test_detail_returns_every_document_together(client, auth):
    """Customization #1: ID, billing and rescue photos seen AT ONCE, not downloaded one by
    one — that is the whole reason the console beats the Django admin here."""
    vr = make_request(docs=["gov_id", "proof_billing", "rescue_photos"])
    body = client.get(f"/admin-api/verifications/{vr.verification_id}", **auth).json()
    assert {d["doc_type"] for d in body["documents"]} == {"gov_id", "proof_billing", "rescue_photos"}


@pytest.mark.django_db
def test_a_purged_document_has_no_url_and_says_when(client, auth):
    """US-SEC4 nulls file_url 90 days after a terminal decision; the ROW survives on purpose.
    A signed URL here would be a broken image pretending to be a document."""
    vr = make_request(docs=["gov_id"])
    doc = vr.documents.first()
    doc.purged_at = timezone.now()
    doc.file_url = ""
    doc.save(update_fields=["purged_at", "file_url"])

    body = client.get(f"/admin-api/verifications/{vr.verification_id}", **auth).json()
    entry = body["documents"][0]
    assert entry["file_url"] is None and entry["purged_at"] is not None


@pytest.mark.django_db
def test_the_tier_checklist_names_what_is_missing(client, auth):
    """Customization #2 — what stops an NGO being approved on rescue-tier evidence."""
    from shelter.models import ShelterProfile
    vr = make_request(docs=["gov_id"])
    ShelterProfile.objects.create(account=vr.account, org_name="X", tier="registered_ngo")

    checklist = client.get(f"/admin-api/verifications/{vr.verification_id}", **auth).json()["checklist"]
    assert checklist["applies"] is True and checklist["tier"] == "registered_ngo"
    assert "proof_billing" in checklist["missing"] and "sec_dti" in checklist["missing"]


@pytest.mark.django_db
def test_a_member_has_no_tier_checklist_rather_than_an_empty_one(client, auth):
    """A Verified Member has no ShelterProfile. An empty `missing` list would read as a
    COMPLETED checklist, which is the opposite of the truth."""
    vr = make_request(vtype="rescuer", name="A Member", docs=["gov_id"])
    checklist = client.get(f"/admin-api/verifications/{vr.verification_id}", **auth).json()["checklist"]
    assert checklist["applies"] is False and checklist["tier"] is None


@pytest.mark.django_db
def test_opening_a_request_writes_an_access_log_row(client, auth, staffer):
    """RA 10173: decisions were already attributable; this covers LOOKING."""
    vr = make_request(docs=["gov_id"])
    assert VerificationAccessLog.objects.filter(verification=vr).count() == 0
    client.get(f"/admin-api/verifications/{vr.verification_id}", **auth)
    log = VerificationAccessLog.objects.filter(verification=vr).get()
    assert log.staff_username == staffer.get_username()
    assert log.viewer_id == staffer.admin_account.account_id


@pytest.mark.django_db
def test_missing_request_is_404(client, auth):
    import uuid
    assert client.get(f"/admin-api/verifications/{uuid.uuid4()}", **auth).status_code == 404


# -- ported from the admin tests US-X2 removed --------------------------------------------
@pytest.mark.django_db
def test_a_document_url_always_comes_from_the_signing_seam(client, auth, monkeypatch):
    """Ported from verifications/tests/test_admin_documents.py, which US-X2 deleted along with
    the surface it tested. The stored `file_url` is an object reference, never something a
    browser should be handed directly — every render must go through `signed_get_url` so the
    URL is short-lived and the bucket stays private."""
    calls = []

    def fake_sign(file_url, **kw):
        calls.append((file_url, kw))
        return f"https://signed.example/{file_url}?exp=300"

    monkeypatch.setattr("adminapi.serializers.signed_get_url", fake_sign)
    vr = make_request(docs=["gov_id"])
    body = client.get(f"/admin-api/verifications/{vr.verification_id}", **auth).json()

    assert calls, "the signing seam was bypassed"
    assert calls[0][1]["expires_in"] == 300, "document URLs must be short-lived"
    assert body["documents"][0]["file_url"].startswith("https://signed.example/")
    assert body["documents"][0]["file_url"] != "s3://x/gov_id.jpg", "raw reference leaked"


@pytest.mark.django_db
def test_each_detail_view_writes_its_own_access_log_row(client, auth):
    """Ported from verifications/tests/test_access_log.py. One row per view, not one per
    request — 'who saw this ID, and when' needs every occasion, not the first."""
    from verifications.models import VerificationAccessLog
    vr = make_request(docs=["gov_id"])
    VerificationAccessLog.objects.all().delete()

    for _ in range(3):
        client.get(f"/admin-api/verifications/{vr.verification_id}", **auth)
    assert VerificationAccessLog.objects.filter(verification=vr).count() == 3


@pytest.mark.django_db
def test_listing_the_queue_writes_no_access_log_row(client, auth):
    """Also ported: the queue shows no documents, so logging a view there would bury the rows
    that record an actual ID being looked at."""
    from verifications.models import VerificationAccessLog
    make_request(docs=["gov_id"])
    VerificationAccessLog.objects.all().delete()
    client.get("/admin-api/verifications", **auth)
    assert VerificationAccessLog.objects.count() == 0
