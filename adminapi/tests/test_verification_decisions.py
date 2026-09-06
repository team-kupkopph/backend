"""US-C3 · decisions delegate to verifications/review.py, and are decided exactly once."""
import itertools

import pytest

from accounts.factories import AccountFactory
from adminapi.tests.conftest import full_signin
from notifications.models import Notification
from verifications.models import AccountCapability, VerificationRequest


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


_seq = itertools.count()


def make_request(vtype="shelter_org", status="pending"):
    # Unique per call: `email` is UNIQUE on account, and a test that creates two requests
    # would otherwise fail on an IntegrityError that says nothing about what it was testing.
    n = next(_seq)
    account = AccountFactory(display_name=f"Alonzo Rescue {n}", email=f"alonzo{n}@ex.com")
    return VerificationRequest.objects.create(account=account, type=vtype, status=status)


def post(client, vr, action, auth, **body):
    return client.post(f"/admin-api/verifications/{vr.verification_id}/{action}",
                       body, content_type="application/json", **auth)


@pytest.mark.django_db
def test_approve_stamps_the_reviewer_and_notifies(client, auth, staffer):
    vr = make_request()
    res = post(client, vr, "approve", auth)
    assert res.status_code == 200

    vr.refresh_from_db()
    assert vr.status == "approved"
    # The decision is attributed to the admin Account, not the Django User — that is the whole
    # point of the staff bridge.
    assert vr.reviewed_by_id == staffer.admin_account.account_id
    assert vr.reviewed_at is not None
    # notify() fired through review.py rather than being re-implemented in the view.
    assert Notification.objects.filter(account=vr.account, type="verification_approved").exists()


@pytest.mark.django_db
def test_approving_a_member_grants_the_rescuer_capability(client, auth):
    """Delegation proof: the capability grant lives in review.py. If the view had its own
    write path this would silently not happen and the approval would look fine."""
    vr = make_request(vtype="rescuer")
    assert post(client, vr, "approve", auth).status_code == 200
    assert AccountCapability.objects.filter(
        account=vr.account, capability="rescuer", status="approved").exists()


@pytest.mark.django_db
def test_reject_requires_a_reason(client, auth):
    """A rejection the applicant cannot act on is a dead end."""
    vr = make_request()
    res = post(client, vr, "reject", auth, notes="   ")
    assert res.status_code == 422 and res.json()["error"]["code"] == "reason_required"
    vr.refresh_from_db()
    assert vr.status == "pending", "a refused decision must not partially apply"


@pytest.mark.django_db
def test_reject_with_a_reason_records_and_notifies(client, auth):
    vr = make_request()
    assert post(client, vr, "reject", auth, notes="Billing proof is illegible.").status_code == 200
    vr.refresh_from_db()
    assert vr.status == "rejected" and "illegible" in vr.notes
    assert Notification.objects.filter(account=vr.account, type="verification_rejected").exists()


@pytest.mark.django_db
def test_needs_info_sets_status_notes_AND_notifies(client, auth):
    """⚠️ Customization #3. Two of the three without the third is the bug: a shelter left
    waiting on a request nobody sent."""
    vr = make_request()
    assert post(client, vr, "needs-info", auth, notes="Send a clearer gov ID.").status_code == 200
    vr.refresh_from_db()
    assert vr.status == "needs_info"
    assert "clearer" in vr.notes
    assert Notification.objects.filter(account=vr.account, type="verification_needs_info").exists()


@pytest.mark.django_db
def test_needs_info_can_still_be_decided_afterwards(client, auth):
    """needs_info is not terminal — the applicant resubmits and the reviewer decides again."""
    vr = make_request()
    post(client, vr, "needs-info", auth, notes="More please.")
    assert post(client, vr, "approve", auth).status_code == 200


@pytest.mark.django_db
def test_a_second_decision_is_409_not_a_silent_overwrite(client, auth):
    vr = make_request()
    assert post(client, vr, "approve", auth).status_code == 200
    res = post(client, vr, "reject", auth, notes="changed my mind")
    assert res.status_code == 409 and res.json()["error"]["code"] == "already_decided"
    vr.refresh_from_db()
    assert vr.status == "approved", "the first decision must stand"


@pytest.mark.django_db(transaction=True)
def test_two_reviewers_deciding_at_once_produce_one_decision(client, staffer):
    """⚠️ The exit criterion, PROVED WITH THREADS rather than asserted.

    Two reviewers opening the same request is normal. Without select_for_update both read
    `pending`, both write, and the second silently overwrites the first reviewer's stamp —
    the applicant gets two notifications and the audit trail records one decision.
    """
    import threading
    from django.db import connection

    token = full_signin(client, staffer)["access"]
    vr = make_request()
    results, lock = [], threading.Lock()

    def decide(action, **body):
        from django.test import Client
        try:
            res = Client().post(
                f"/admin-api/verifications/{vr.verification_id}/{action}",
                body, content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}")
            with lock:
                results.append(res.status_code)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=decide, args=("approve",)),
        threading.Thread(target=decide, args=("reject",), kwargs={"notes": "no"}),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409], f"expected exactly one winner, got {results}"
    vr.refresh_from_db()
    assert vr.status in ("approved", "rejected")
    # One decision means one notification, not two.
    assert Notification.objects.filter(account=vr.account).count() == 1


@pytest.mark.django_db
def test_a_decision_needs_a_staff_token(client):
    vr = make_request()
    res = client.post(f"/admin-api/verifications/{vr.verification_id}/approve",
                      {}, content_type="application/json")
    assert res.status_code in (401, 403)


# -- US-R6 parity · per-document rejection (found while auditing US-X2) --------------------
@pytest.mark.django_db
def test_needs_info_can_reject_individual_documents(client, auth, staffer):
    """⚠️ The capability the Django admin had and the console did not.

    US-R6 exists so a reviewer bounces ONE photo instead of the whole set, and the applicant's
    resubmit loop needs to know which file failed. Switching the admin off without this would
    have deleted the only way to do it.
    """
    from verifications.models import VerificationDocument
    vr = make_request()
    good = VerificationDocument.objects.create(verification=vr, doc_type="gov_id",
                                               file_url="s3://a.jpg")
    bad = VerificationDocument.objects.create(verification=vr, doc_type="proof_billing",
                                              file_url="s3://b.jpg")

    res = post(client, vr, "needs-info", auth,
               notes="One file needs replacing.",
               documents=[{"document_id": str(bad.document_id), "note": "Illegible."}])
    assert res.status_code == 200

    bad.refresh_from_db()
    good.refresh_from_db()
    assert bad.status == "rejected" and bad.review_note == "Illegible."
    assert bad.reviewed_by_id == staffer.admin_account.account_id
    assert good.status == "pending", "an unnamed document must not be touched"
    vr.refresh_from_db()
    assert vr.status == "needs_info"


@pytest.mark.django_db
def test_a_missing_per_file_reason_refuses_the_WHOLE_bounce(client, auth):
    """Atomic with the request bounce: half-applied is the state the applicant must never see."""
    from verifications.models import VerificationDocument
    vr = make_request()
    doc = VerificationDocument.objects.create(verification=vr, doc_type="gov_id",
                                              file_url="s3://a.jpg")

    res = post(client, vr, "needs-info", auth, notes="Please fix.",
               documents=[{"document_id": str(doc.document_id), "note": "   "}])
    assert res.status_code == 422

    doc.refresh_from_db()
    vr.refresh_from_db()
    assert doc.status == "pending", "the document rejection must have rolled back"
    assert vr.status == "pending", "the bounce must have rolled back too"


@pytest.mark.django_db
def test_a_document_from_another_request_cannot_be_rejected(client, auth):
    """The endpoint is scoped to its own request — otherwise it is an oracle for, and a lever
    on, another applicant's files."""
    from verifications.models import VerificationDocument
    mine = make_request()
    theirs = make_request()
    other_doc = VerificationDocument.objects.create(verification=theirs, doc_type="gov_id",
                                                   file_url="s3://x.jpg")

    res = post(client, mine, "needs-info", auth, notes="hi",
               documents=[{"document_id": str(other_doc.document_id), "note": "nope"}])
    assert res.status_code == 422
    other_doc.refresh_from_db()
    assert other_doc.status == "pending"


@pytest.mark.django_db
def test_needs_info_without_documents_still_works(client, auth):
    """The common case — bounce the request without singling out a file."""
    vr = make_request()
    assert post(client, vr, "needs-info", auth, notes="Send a clearer ID.").status_code == 200
    vr.refresh_from_db()
    assert vr.status == "needs_info"
