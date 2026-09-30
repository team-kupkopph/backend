"""US-S6 · the single writer for a stray report's status.

Sprint 3 builds the claim / rescue / resolve transitions on top of this. The point of routing
every status move through one helper is the invariant it enforces: a report's status can never
change without a `case_status_history` row recording who moved it and why — the report and the
audit row commit (or roll back) together.
"""
from django.db import transaction
from django.utils import timezone

from .models import CaseStatusHistory, RescueCase, StrayStatus


class StatusError(ValueError):
    """A status move that can't be applied — e.g. an unknown target status."""


@transaction.atomic
def set_report_status(report, status, by, note=""):
    """Move `report` to `status` and log it, atomically.

    `by` is the acting Account (nullable-safe — SET_NULL on the history row if that account is
    later deleted). `note` is a short human reason (≤200 chars, DDL-capped). Returns the created
    `CaseStatusHistory` row. Raises `StatusError` for a status outside `StrayStatus`.
    """
    if status not in StrayStatus.values:
        raise StatusError(f"unknown stray status: {status!r}")
    report.status = status
    report.save(update_fields=["status", "updated_at"])
    return CaseStatusHistory.objects.create(
        report=report, status=status, changed_by_account=by, note=note or "",
    )


@transaction.atomic
def resolve_report(report, by, note=""):
    """Close a rescue: move `report` to `resolved` AND stamp its active case resolved, together.

    For the resolutions that happen OUTSIDE the claimer's own status screen — a direct
    placement accepted by its recipient (S2, dev/sagip-build-review.md). Moving only the
    status left the case open (`resolved_at` NULL), so the rescuer's Home kept offering to
    "Find them a home" for an animal that already had one. Idempotent: an already-resolved
    report is left alone and None is returned; otherwise the new history row is.
    """
    if report.status == StrayStatus.RESOLVED:
        return None
    history = set_report_status(report, StrayStatus.RESOLVED, by, note=note)
    RescueCase.objects.filter(report=report, expired_at__isnull=True,
                              resolved_at__isnull=True).update(resolved_at=timezone.now())
    return history
