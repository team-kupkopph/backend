"""Tier-derived required-doc rule (US-R4). One source of §3.5, imported by both the
submit path (`_shelter_doc_error`) and the reviewer's missing-docs line."""
from verifications.rules import deferred_docs, missing_docs, required_doc_types, review_checklist

TIER1_FULL = ["gov_id", "proof_billing", "rescue_photos", "rescue_photos", "rescue_photos"]


def test_required_doc_types_tier1():
    assert required_doc_types("community_rescue") == ["gov_id", "proof_billing", "rescue_photos"]


def test_required_doc_types_tier2_adds_ngo_papers():
    assert required_doc_types("registered_ngo") == [
        "gov_id", "proof_billing", "rescue_photos", "sec_dti", "bai_cert"]


def test_tier1_full_set_is_not_missing_anything():
    assert missing_docs("community_rescue", TIER1_FULL) == []


def test_tier1_missing_proof_billing():
    present = ["gov_id", "rescue_photos", "rescue_photos", "rescue_photos"]
    assert missing_docs("community_rescue", present) == ["proof_billing"]


def test_tier1_fewer_than_three_photos_counts_as_missing():
    present = ["gov_id", "proof_billing", "rescue_photos", "rescue_photos"]
    assert "rescue_photos" in missing_docs("community_rescue", present)


def test_tier2_missing_both_ngo_papers():
    assert missing_docs("registered_ngo", TIER1_FULL) == ["sec_dti", "bai_cert"]


def test_tier2_bai_pending_makes_bai_cert_deferred_not_missing():
    present = TIER1_FULL + ["sec_dti"]
    assert missing_docs("registered_ngo", present, bai_pending=True) == []
    assert deferred_docs("registered_ngo", present, bai_pending=True) == ["bai_cert"]


def test_tier2_bai_not_pending_and_absent_is_missing_not_deferred():
    present = TIER1_FULL + ["sec_dti"]
    assert missing_docs("registered_ngo", present, bai_pending=False) == ["bai_cert"]
    assert deferred_docs("registered_ngo", present, bai_pending=False) == []


def test_tier2_full_set_present_nothing_missing_or_deferred():
    present = TIER1_FULL + ["sec_dti", "bai_cert"]
    assert missing_docs("registered_ngo", present, bai_pending=True) == []
    assert deferred_docs("registered_ngo", present, bai_pending=True) == []


# ── review_checklist: the reviewer's read of an already-submitted request ──────────
# The submit gate never accepts a tier-2 request that is genuinely missing bai_cert, so
# at review time an absent bai_cert is *deferred*, never missing (no bai_pending column
# needed — it's implied by the request existing at all).

def test_review_checklist_tier1_full_is_clear():
    assert review_checklist("community_rescue", TIER1_FULL) == ([], [])


def test_review_checklist_tier2_full_is_clear():
    present = TIER1_FULL + ["sec_dti", "bai_cert"]
    assert review_checklist("registered_ngo", present) == ([], [])


def test_review_checklist_tier2_absent_bai_reads_as_deferred_not_missing():
    present = TIER1_FULL + ["sec_dti"]
    missing, deferred = review_checklist("registered_ngo", present)
    assert missing == []
    assert deferred == ["bai_cert"]


def test_review_checklist_surfaces_a_genuinely_absent_required_doc():
    # defensive: a reviewer must still see a hole the gate shouldn't have let through
    present = ["gov_id", "proof_billing", "rescue_photos"]  # base only, no sec_dti
    missing, deferred = review_checklist("registered_ngo", present)
    assert "sec_dti" in missing
    assert deferred == ["bai_cert"]
