"""C13 / D10 · what a moderation takedown does to a Sagip report (called by
moderation.actions.resolve_flag when a `report` flag is actioned), and U1 · the staff restore
that undoes a mistaken one."""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from notifications.service import notify
from sagip.models import (
    MatchStatus,
    OfferStatus,
    ReportMatch,
    ReportOffer,
    RescueCase,
    StrayReport,
    StrayStatus,
)
from sagip.status import set_report_status


class ReportNotHidden(Exception):
    """`restore_report` on a report no takedown is holding."""


@transaction.atomic
def hide_report(report, by):
    # Lock order: case, then report — the same as CaseStatusView, CaseReleaseView and _expire_case,
    # so a takedown that overlaps a status update or a lapse can't deadlock.
    case = (RescueCase.objects.select_for_update()
            .filter(report_id=report.pk, expired_at__isnull=True).first())
    report = StrayReport.objects.select_for_update().get(pk=report.pk)
    if report.hidden_at is not None:
        return
    if report.status == StrayStatus.CLAIMED and case is None:
        # C13 · the one documented exception to case-then-report: the lookup above can't see a
        # case whose claim hasn't committed yet, so it found nothing to lock; we then blocked on
        # the report lock (the claim holds it) and woke to a claimed report with no case in hand. Re-query now that the claim has committed.
        # The only competitor for a brand-new case is vanishingly rare, and Postgres detects any
        # deadlock rather than hanging.
        case = (RescueCase.objects.select_for_update()
                .filter(report_id=report.pk, expired_at__isnull=True).first())
    now = timezone.now()
    report.hidden_at = now
    report.save(update_fields=["hidden_at"])
    ReportOffer.objects.filter(report=report, status=OfferStatus.OPEN).update(
        status=OfferStatus.EXPIRED)
    # C13 · an undecided lost & found pair with a removed side is no longer a lead; a decided
    # one (confirmed or dismissed) is history and stays as it was.
    ReportMatch.objects.filter(Q(report=report) | Q(matched_report=report),
                               status=MatchStatus.SUGGESTED).update(status=MatchStatus.DISMISSED)
    # A claim nobody has acted on ends; an animal already in someone's care stays with them.
    if report.status == StrayStatus.CLAIMED and case is not None:
        case.expired_at = now
        case.save(update_fields=["expired_at"])
        # C13 · the claim turned the report's open offers MATCHED; with the claim gone they'd
        # stay MATCHED forever. A report in someone's custody keeps its offers.
        ReportOffer.objects.filter(report=report, status=OfferStatus.MATCHED).update(
            status=OfferStatus.EXPIRED)
        notify(case.claimed_by_account, "report_removed", title="A report you claimed was removed",
               body="Our moderators removed it, so there's nothing left to do on it.",
               data={"report_id": str(report.pk)})


@transaction.atomic
def restore_report(report, by, reason):
    """U1 · staff un-hide a report a takedown took down in error; the way back for `hide_report`.

    It clears `hidden_at` and nothing else the takedown did: expired offers stay expired,
    dismissed matches stay dismissed, and an ended case stays ended (a claim nobody can still
    vouch for is not resurrected), and nobody is notified. The one repair is the status: a
    takedown that ended an unacted claim left the report `claimed` with no active case, a state
    that is on no map and claimable by no one, so it goes back to `reported` through
    `set_report_status` (history row, note `restored_by_moderation` — never the reason) and the next rescuer
    can claim it. A report in someone's custody (`rescued`, `safe`, `resolved`) keeps its status
    and its case. Raises `ReportNotHidden` when there is nothing to restore."""
    # Lock order: case, then report — as `hide_report`, so a restore that overlaps a status
    # update or a lapse can't deadlock. No re-query for a not-yet-committed case here: nothing
    # can claim a hidden report, so no new case can appear while it is hidden.
    case = (RescueCase.objects.select_for_update()
            .filter(report_id=report.pk, expired_at__isnull=True).first())
    report = StrayReport.objects.select_for_update().get(pk=report.pk)
    if report.hidden_at is None:
        raise ReportNotHidden()
    report.hidden_at = None
    report.save(update_fields=["hidden_at", "updated_at"])
    if report.status == StrayStatus.CLAIMED and case is None:
        # The note is the bare marker: the reason belongs to the audit, and `status_history` is
        # the reporter's own timeline, which a moderator's internal reason must never reach.
        set_report_status(report, StrayStatus.REPORTED, by, note="restored_by_moderation")
    return report
