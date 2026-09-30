"""Who is asked to help, and when (D2 / S6 · S16, dev/sagip-build-review.md).

Owner decision D2 (2026-09-30): an injured, sick or pregnant animal alerts verified rescuers
AND verified shelters in the report's city the moment it is reported, at most DAILY_CAP of
these alerts per person per rolling 24 h. A healthy stray alerts no one here; it waits for
the normal escalation (sweeps.escalate_reports), which reaches the same city audience later.
Before this, the first push for an injured animal went out two hours after it was reported.
"""
from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import Account, AccountStatus
from common.cities import city_variants
from notifications.models import Notification
from notifications.service import notify
from sagip.models import ReportType, StrayCondition

URGENT_CONDITIONS = {StrayCondition.INJURED, StrayCondition.SICK, StrayCondition.PREGNANT}
# A found animal is loose and needs holding, like a stray. A lost pet is its owner's to find
# and isn't claimable (D6), so it pages no one.
ALERTED_TYPES = {ReportType.STRAY, ReportType.FOUND}
# D2's cap, per person per rolling 24 h. A policy number — move it deliberately. Escalations
# (report_escalated) are NOT capped: they are the safety net for a report nobody took.
DAILY_CAP = 5
ALERT_TYPE = "report_nearby"


def verified_in_city(city):
    """Active accounts that can act on a stray in `city`: an approved rescuer capability
    (Verified Member) or an approved shelter_org verification (verified shelter) — the same
    pair `IsVerifiedRescuer` lets claim — whose PRIMARY address is in that city.

    ⚠️ Compared through city_variants: the report's city comes from the phone's reverse-
    geocoder ("Marikina"), an address's from the location picker ("Marikina City"). An exact
    match paged no one in Marikina or Pasig (S4). No city, no audience."""
    variants = city_variants(city)
    if not variants:
        return Account.objects.none()
    same_city = Q()
    for variant in variants:
        same_city |= Q(addresses__city__iexact=variant)
    can_act = (Q(capabilities__capability="rescuer", capabilities__status="approved")
               | Q(verifications__type="shelter_org", verifications__status="approved"))
    return (Account.objects
            .filter(same_city, can_act, addresses__is_primary=True, status=AccountStatus.ACTIVE)
            .distinct())


def alerts_at_report_apply(report):
    """Whether D2's policy alerts anyone for this report at all."""
    return report.report_type in ALERTED_TYPES and report.condition in URGENT_CONDITIONS


def already_alerted_ids(report):
    """Accounts already asked about THIS report, by the report-time alert."""
    return (Notification.objects
            .filter(type=ALERT_TYPE, data__report_id=str(report.pk))
            .values_list("account_id", flat=True))


def alert_at_report(report, now=None):
    """D2 · page the city's verified rescuers and shelters about an urgent report. Returns the
    number alerted, or None when the policy doesn't apply. The reporter is never paged about
    their own report; anyone already at DAILY_CAP today is skipped (the report is still on
    their map). Callers treat this as best-effort — a failure here must never lose a report."""
    if not alerts_at_report_apply(report):
        return None
    now = now or timezone.now()
    recipients = list(verified_in_city(report.city).exclude(pk=report.reporter_account_id))
    if not recipients:
        return 0
    used = dict(Notification.objects
                .filter(account__in=recipients, type=ALERT_TYPE,
                        created_at__gte=now - timezone.timedelta(hours=24))
                .values("account").annotate(n=Count("pk")).values_list("account", "n"))
    species = report.get_species_display().lower()
    condition = report.get_condition_display().lower()
    sent = 0
    for account in recipients:
        if used.get(account.pk, 0) >= DAILY_CAP:
            continue
        notify(account, ALERT_TYPE, title=f"A {condition} {species} near you needs help",
               body=f"Just reported in {report.city}. Open it to claim it or offer help.",
               data={"report_id": str(report.pk)})
        sent += 1
    return sent
