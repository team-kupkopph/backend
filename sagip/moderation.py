"""C13 / D10 · what a moderation takedown does to a Sagip report (called by
moderation.actions.resolve_flag when a `report` flag is actioned)."""
from django.db import transaction
from django.utils import timezone

from notifications.service import notify
from sagip.models import OfferStatus, ReportOffer, RescueCase, StrayReport, StrayStatus


@transaction.atomic
def hide_report(report, by):
    report = StrayReport.objects.select_for_update().get(pk=report.pk)
    if report.hidden_at is not None:
        return
    now = timezone.now()
    report.hidden_at = now
    report.save(update_fields=["hidden_at"])
    ReportOffer.objects.filter(report=report, status=OfferStatus.OPEN).update(
        status=OfferStatus.EXPIRED)
    # A claim nobody has acted on ends; an animal already in someone's care stays with them.
    if report.status == StrayStatus.CLAIMED:
        case = (RescueCase.objects.select_for_update()
                .filter(report=report, expired_at__isnull=True).first())
        if case is not None:
            case.expired_at = now
            case.save(update_fields=["expired_at"])
            notify(case.claimed_by_account, "report_removed", title="A report you claimed was removed",
                   body="Our moderators removed it, so there's nothing left to do on it.",
                   data={"report_id": str(report.pk)})
