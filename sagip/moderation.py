"""C13 / D10 · what a moderation takedown does to a Sagip report (called by
moderation.actions.resolve_flag when a `report` flag is actioned)."""
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
