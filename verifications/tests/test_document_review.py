"""US-R6 — per-document decisions + the needs-info bounce.

A reviewer can clear three files and reject only the fourth (with a per-file reason the
applicant sees), then send the whole request back for more info.
"""
import pytest

from accounts.factories import AccountFactory
from verifications.models import VerificationDocument, VerificationRequest
from verifications.review import ReviewError, request_more_info, review_document


def _reviewer():
    return AccountFactory(account_type="admin", email="rev@kupkop.ph")


def _doc(vr, doc_type="proof_billing"):
    return VerificationDocument.objects.create(
        verification=vr, doc_type=doc_type, file_url=f"https://example.invalid/{doc_type}")


def _request():
    return VerificationRequest.objects.create(account=AccountFactory(), type="shelter_org")


@pytest.mark.django_db
def test_review_document_approve_stamps_status_and_reviewer():
    reviewer = _reviewer()
    doc = _doc(_request())
    review_document(doc, reviewer, "approved")
    doc.refresh_from_db()
    assert doc.status == "approved"
    assert doc.reviewed_by == reviewer and doc.reviewed_at is not None


@pytest.mark.django_db
def test_review_document_reject_requires_a_per_file_reason():
    reviewer = _reviewer()
    doc = _doc(_request())
    with pytest.raises(ReviewError):
        review_document(doc, reviewer, "rejected", note="")
    doc.refresh_from_db()
    assert doc.status == "pending"  # unchanged — a reason-less rejection is refused


@pytest.mark.django_db
def test_review_document_reject_records_the_reason_shown_to_the_applicant():
    reviewer = _reviewer()
    doc = _doc(_request())
    review_document(doc, reviewer, "rejected", note="Photo is too dark to read the address.")
    doc.refresh_from_db()
    assert doc.status == "rejected"
    assert doc.review_note == "Photo is too dark to read the address."
    assert doc.reviewed_by == reviewer and doc.reviewed_at is not None


@pytest.mark.django_db
def test_review_document_rejects_an_unknown_decision():
    reviewer = _reviewer()
    doc = _doc(_request())
    with pytest.raises(ReviewError):
        review_document(doc, reviewer, "maybe")


@pytest.mark.django_db
def test_request_more_info_moves_request_to_needs_info_with_a_note():
    reviewer = _reviewer()
    vr = _request()
    request_more_info(vr, reviewer, "Please replace the billing photo.")
    vr.refresh_from_db()
    assert vr.status == "needs_info"
    assert vr.notes == "Please replace the billing photo."
    assert vr.reviewed_by == reviewer and vr.reviewed_at is not None


@pytest.mark.django_db
def test_request_more_info_requires_a_note():
    reviewer = _reviewer()
    vr = _request()
    with pytest.raises(ReviewError):
        request_more_info(vr, reviewer, "   ")
    vr.refresh_from_db()
    assert vr.status == "pending"
