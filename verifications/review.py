"""Verification decisions (US-R5, extended by US-R6).

The state changes a reviewer's decision makes — approve, reject — live here as a service,
independent of the Django-admin UI that invokes them, so the transitions are unit-testable
and the "approved *is* the gate" rule (Decision B / US-X2) has a single writer. `reviewer`
is the admin `Account` the decision is attributed to (resolved from the acting staff user
via `accounts.staff.reviewer_account`).
"""
from django.db import transaction
from django.utils import timezone

from notifications.service import notify
from verifications.models import AccountCapability, VerificationStatus, VerificationType


class ReviewError(Exception):
    """A decision that can't be applied as asked — e.g. a rejection with no reason."""


def _notify_decision(vr, ntype, title, body):
    """Tell the applicant about a decision (US-X1). In-app row this sprint; Sprint 5 adds
    push. Called inside the decision's transaction, so the row commits with the decision
    (and rolls back with it if the decision is refused)."""
    notify(vr.account, ntype, title=title, body=body,
           data={"verification_id": str(vr.verification_id), "type": vr.type})


@transaction.atomic
def approve_request(vr, reviewer):
    """Approve a verification request and stamp the reviewer.

    For a Verified Member (`type='rescuer'`) this also grants the rescuer capability — no
    separate gate flag is written; the approved request/capability *is* the gate.
    """
    vr.status = VerificationStatus.APPROVED
    vr.reviewed_by = reviewer
    vr.reviewed_at = timezone.now()
    vr.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    if vr.type == VerificationType.RESCUER:
        AccountCapability.objects.update_or_create(
            account=vr.account, capability="rescuer",
            defaults={"status": "approved", "granted_at": timezone.now()})
    if vr.type == VerificationType.SHELTER_ORG:
        _maybe_upgrade_tier(vr)
    body = ("Your shelter verification was approved." if vr.type == VerificationType.SHELTER_ORG
            else "Your Verified Member application was approved.")
    _notify_decision(vr, "verification_approved", "You're verified", body)
    return vr


def _maybe_upgrade_tier(vr):
    """US-X4 · approving a shelter_org request that carries the NGO papers (`sec_dti`) promotes
    the org to registered_ngo. The tier moves only on APPROVAL — never at submit — so the
    Verified Shelter badge can't precede the decision and a tier-1 shelter keeps its tier-1
    standing while the upgrade is under review. A no-op for an org already at registered_ngo
    (the initial tier-2 path) or a request with no NGO evidence (a plain tier-1 approval)."""
    from shelter.models import ShelterProfile, ShelterTier
    if not vr.documents.filter(doc_type="sec_dti").exists():
        return
    profile = ShelterProfile.objects.filter(account=vr.account).first()
    if profile and profile.tier != ShelterTier.REGISTERED_NGO:
        profile.tier = ShelterTier.REGISTERED_NGO
        profile.save(update_fields=["tier"])


@transaction.atomic
def reject_request(vr, reviewer, notes):
    """Reject a request with a reason. The reason is shown to the applicant, so an empty
    one is refused — a rejection they can't act on is a dead end."""
    reason = (notes or "").strip()
    if not reason:
        raise ReviewError("A rejection needs a reason the applicant can act on.")
    vr.status = VerificationStatus.REJECTED
    vr.notes = reason
    vr.reviewed_by = reviewer
    vr.reviewed_at = timezone.now()
    vr.save(update_fields=["status", "notes", "reviewed_by", "reviewed_at"])
    _notify_decision(vr, "verification_rejected", "Verification not approved", reason)
    return vr


DOC_DECISIONS = {"approved", "rejected"}


@transaction.atomic
def review_document(doc, reviewer, decision, note=""):
    """Approve or reject a single document (US-R6). A rejection needs a per-file reason —
    it is the note shown to the applicant on that file. Clearing three files and rejecting
    one is how a reviewer bounces a single photo instead of the whole set."""
    if decision not in DOC_DECISIONS:
        raise ReviewError(f"Unknown document decision: {decision!r}")
    reason = (note or "").strip()
    if decision == "rejected" and not reason:
        raise ReviewError("A rejected file needs a reason the applicant can act on.")
    doc.status = decision
    doc.review_note = reason
    doc.reviewed_by = reviewer
    doc.reviewed_at = timezone.now()
    doc.save(update_fields=["status", "review_note", "reviewed_by", "reviewed_at"])
    return doc


@transaction.atomic
def request_more_info(vr, reviewer, notes):
    """Bounce a request back to the applicant for more info (US-R6): status -> needs_info
    with an overall note. The per-file reasons live on the documents; this is the summary
    ask. (Notifying the applicant is US-X1 — in-app only this sprint.)"""
    reason = (notes or "").strip()
    if not reason:
        raise ReviewError("Asking for more info needs a note the applicant can act on.")
    vr.status = VerificationStatus.NEEDS_INFO
    vr.notes = reason
    vr.reviewed_by = reviewer
    vr.reviewed_at = timezone.now()
    vr.save(update_fields=["status", "notes", "reviewed_by", "reviewed_at"])
    _notify_decision(vr, "verification_needs_info", "More information needed", reason)
    return vr
