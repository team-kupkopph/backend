"""Shapes the console reads. Field names mirror the models so `packages/contract` can be
compared against them (US-A4's contract:check)."""
from common.storage import signed_get_url
from shelter.models import ShelterProfile
from verifications.rules import review_checklist

# ⚠️ 5 minutes. `common.storage.SIGNED_URL_TTL` is the ceiling; document URLs reach government
# IDs, so they get the shortest useful life rather than the default.
DOCUMENT_URL_TTL = 300


def queue_row(vr):
    return {
        "verification_id": str(vr.verification_id),
        "type": vr.type,
        "status": vr.status,
        "submitted_at": vr.submitted_at.isoformat(),
        "applicant": {
            "account_id": str(vr.account.account_id),
            "display_name": vr.account.display_name,
            "email": vr.account.email,
        },
        # The queue annotates this; the detail view does not (it has the documents in hand,
        # and a second COUNT query per row would be waste). Falling back rather than requiring
        # the annotation keeps one row shape for both callers.
        "document_count": getattr(vr, "document_count", None) or vr.documents.count(),
    }


def document(doc, *, include_url=True):
    """One document row.

    ⚠️ `file_url` is null once the image is purged — US-SEC4 nulls it 90 days after a terminal
    decision under RA 10173 data minimisation, and the ROW deliberately survives so the audit
    trail keeps "and here is when it stopped existing". The console renders that as an explicit
    purged state; a signed URL for a purged object would be a broken image pretending to be a
    document.
    """
    purged = doc.purged_at is not None
    return {
        "document_id": str(doc.document_id),
        "doc_type": doc.doc_type,
        "status": doc.status,
        "review_note": doc.review_note,
        "uploaded_at": doc.uploaded_at.isoformat(),
        "purged_at": doc.purged_at.isoformat() if purged else None,
        "superseded_by": str(doc.superseded_by_id) if doc.superseded_by_id else None,
        "file_url": (
            signed_get_url(doc.file_url, expires_in=DOCUMENT_URL_TTL)
            if include_url and not purged and doc.file_url
            else None
        ),
    }


def detail(vr):
    """The review screen's whole payload: request, applicant, documents, and the checklist."""
    profile = ShelterProfile.objects.filter(account=vr.account).first()
    documents = list(vr.documents.all().order_by("uploaded_at"))
    present = [d.doc_type for d in documents]

    # ⚠️ The tier-aware checklist — customization #2 of the three
    # dev/verification-and-admin.md calls "small, but not optional". It is what stops an NGO
    # being approved on rescue-tier evidence. Derived from verifications/rules.py, the single
    # source the SUBMIT path uses too: two copies would drift, and the failure mode is a
    # wrongly-granted Verified Shelter badge.
    if profile is not None:
        missing, deferred = review_checklist(profile.tier, present)
        tier = profile.tier
    else:
        # A Verified Member has no ShelterProfile, so there is no tier document set to check
        # against. Say that explicitly rather than reporting an empty "nothing missing", which
        # reads as a completed checklist.
        missing, deferred, tier = [], [], None

    return {
        **queue_row(vr),
        "notes": vr.notes,
        "reviewed_at": vr.reviewed_at.isoformat() if vr.reviewed_at else None,
        "social_proof_url": vr.social_proof_url,
        "tier": tier,
        "checklist": {
            "tier": tier,
            "missing": missing,
            "deferred": deferred,
            "applies": tier is not None,
        },
        "documents": [document(d) for d in documents],
    }
