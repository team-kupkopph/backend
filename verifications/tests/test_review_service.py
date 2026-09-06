import pytest

from accounts.factories import AccountFactory
from verifications.models import AccountCapability, VerificationRequest
from verifications.review import ReviewError, approve_request, reject_request


def _reviewer():
    return AccountFactory(account_type="admin", email="rev@kupkop.ph")


@pytest.mark.django_db
def test_approve_sets_status_reviewer_and_timestamp():
    reviewer = _reviewer()
    vr = VerificationRequest.objects.create(account=AccountFactory(), type="shelter_org")
    approve_request(vr, reviewer)
    vr.refresh_from_db()
    assert vr.status == "approved"
    assert vr.reviewed_by == reviewer
    assert vr.reviewed_at is not None


@pytest.mark.django_db
def test_approve_rescuer_grants_the_pending_capability():
    reviewer = _reviewer()
    applicant = AccountFactory()
    vr = VerificationRequest.objects.create(account=applicant, type="rescuer")
    cap = AccountCapability.objects.create(account=applicant, capability="rescuer",
                                           status="pending")
    approve_request(vr, reviewer)
    cap.refresh_from_db()
    assert cap.status == "approved"
    assert cap.granted_at is not None


@pytest.mark.django_db
def test_approve_shelter_org_does_not_create_a_capability():
    reviewer = _reviewer()
    vr = VerificationRequest.objects.create(account=AccountFactory(), type="shelter_org")
    approve_request(vr, reviewer)
    assert not AccountCapability.objects.filter(account=vr.account).exists()


@pytest.mark.django_db
def test_reject_sets_status_notes_reviewer_and_timestamp():
    reviewer = _reviewer()
    vr = VerificationRequest.objects.create(account=AccountFactory(), type="shelter_org")
    reject_request(vr, reviewer, "Proof of billing is unreadable — please re-upload.")
    vr.refresh_from_db()
    assert vr.status == "rejected"
    assert vr.notes == "Proof of billing is unreadable — please re-upload."
    assert vr.reviewed_by == reviewer
    assert vr.reviewed_at is not None


@pytest.mark.django_db
def test_reject_with_empty_notes_is_refused_and_does_not_mutate():
    reviewer = _reviewer()
    vr = VerificationRequest.objects.create(account=AccountFactory(), type="shelter_org")
    with pytest.raises(ReviewError):
        reject_request(vr, reviewer, "")
    vr.refresh_from_db()
    assert vr.status == "pending"  # a rejection the applicant can't act on is refused


@pytest.mark.django_db
def test_reject_with_whitespace_only_notes_is_refused():
    reviewer = _reviewer()
    vr = VerificationRequest.objects.create(account=AccountFactory(), type="shelter_org")
    with pytest.raises(ReviewError):
        reject_request(vr, reviewer, "   ")
    vr.refresh_from_db()
    assert vr.status == "pending"
