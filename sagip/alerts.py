"""Who is asked to help, and when (D2 / S6 · S16, dev/sagip-build-review.md).

Owner decision D2 (2026-09-30): an injured, sick or pregnant animal alerts verified rescuers
AND verified shelters in the report's city the moment it is reported, at most DAILY_CAP of
these alerts per person per rolling 24 h. A healthy stray alerts no one here; it waits for
the normal escalation (sweeps.escalate_reports), which reaches the same city audience later.
Before this, the first push for an injured animal went out two hours after it was reported.
"""
from collections import Counter

from django.db.models import Q
from django.utils import timezone

from accounts.models import Account, AccountStatus
from common.cities import city_variants
from notifications.models import Notification
from notifications.service import notify
from sagip.models import ReportType, StrayCondition, StrayReport
from sagip.notices import with_article

URGENT_CONDITIONS = {StrayCondition.INJURED, StrayCondition.SICK, StrayCondition.PREGNANT}
# A found animal is loose and needs holding, like a stray. A lost pet is its owner's to find
# and isn't claimable (D6), so it pages no one.
ALERTED_TYPES = {ReportType.STRAY, ReportType.FOUND}
# D2's cap, per person per rolling 24 h. A policy number — move it deliberately. Escalations
# (report_escalated) are NOT capped: they are the safety net for a report nobody took.
DAILY_CAP = 5
ALERT_TYPE = "report_nearby"
# D9 (2026-10-01) · a single reporter's urgent reports page people at most this many times per
# rolling 24 h, so a handful of fake reports can't use up every rescuer's DAILY_CAP.
REPORTER_DAILY_CAP = 3
# A report its reporter closed as a mistake (views.CLOSE_NOTE_PREFIX + "mistake") doesn't count
# against anyone's cap, and neither does a hidden one (C13).
VOID_CLOSE_NOTE = "closed_by_reporter:mistake"


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


def hold_reason(report, now=None):
    """D9 · why this report must NOT page people now, or None."""
    now = now or timezone.now()
    reporter = report.reporter_account
    if reporter is None or not reporter.phone_verified_at:
        return "phone_unverified"
    since = now - timezone.timedelta(hours=24)
    theirs = [str(pk) for pk in StrayReport.objects
              .filter(reporter_account=reporter, created_at__gte=since)
              .exclude(pk=report.pk).values_list("pk", flat=True)]
    if not theirs:
        return None
    paged = set(Notification.objects
                .filter(type=ALERT_TYPE, data__report_id__in=theirs)
                .exclude(data__has_key="reopened")
                .values_list("data__report_id", flat=True))
    return "reporter_cap" if len(paged) >= REPORTER_DAILY_CAP else None


def _void_report_ids(report_ids):
    if not report_ids:
        return set()
    rows = (StrayReport.objects.filter(pk__in=list(report_ids))
            .filter(Q(hidden_at__isnull=False) | Q(status_history__note=VOID_CLOSE_NOTE))
            .values_list("pk", flat=True))
    return {str(pk) for pk in rows}


def alert_at_report(report, now=None):
    """D2 · page the city's verified rescuers and shelters about an urgent report. Returns the
    number alerted, or None when the policy doesn't apply. The reporter is never paged about
    their own report; anyone already at DAILY_CAP today is skipped (the report is still on
    their map). Callers treat this as best-effort — a failure here must never lose a report."""
    if not alerts_at_report_apply(report):
        return None
    held = hold_reason(report, now)
    if held:
        report.alert_held = held
        report.save(update_fields=["alert_held"])
        return 0
    species = report.get_species_display().lower()
    condition = report.get_condition_display().lower()
    return _page(verified_in_city(report.city).exclude(pk=report.reporter_account_id),
                 title=f"{with_article(f'{condition} {species}')} near you needs help",
                 body=f"Just reported in {report.city}. Open it to claim it or offer help.",
                 data={"report_id": str(report.pk)}, now=now)


def alert_on_reopen(report, released_by=None, now=None):
    """Re-alert · a claimed report went back on the map (its claimer released it, D3, or the claim
    lapsed). Page the city's verified rescuers and shelters who have NOT been asked about this
    report yet — not at report time, not by escalation, not by an earlier reopen — never the
    reporter or the claimer who just let go. Same policy and cap as the first alert (urgent
    strays and found animals only); rows carry `reopened: True` so the reporter's counts keep
    "alerted right away" true. Returns the number paged, or None when the policy doesn't apply."""
    if report.hidden_at is not None:
        return None
    if not alerts_at_report_apply(report):
        return None
    asked = set(already_alerted_ids(report)) | set(
        Notification.objects.filter(type="report_escalated", data__report_id=str(report.pk))
        .values_list("account_id", flat=True))
    skip = asked | {report.reporter_account_id, getattr(released_by, "pk", released_by)}
    species = report.get_species_display().lower()
    condition = report.get_condition_display().lower()
    return _page(verified_in_city(report.city).exclude(pk__in=[s for s in skip if s]),
                 title=f"{with_article(f'{condition} {species}')} near you needs help again",
                 body=f"The rescuer who claimed it in {report.city} couldn't go. "
                      f"Open it to claim it or offer help.",
                 data={"report_id": str(report.pk), "reopened": True}, now=now)


def _page(recipients_qs, *, title, body, data, now=None):
    """Send one report_nearby to each recipient still under DAILY_CAP for the rolling 24 h."""
    now = now or timezone.now()
    recipients = list(recipients_qs)
    if not recipients:
        return 0
    since = now - timezone.timedelta(hours=24)
    recent = list(Notification.objects
                  .filter(account__in=recipients, type=ALERT_TYPE, created_at__gte=since)
                  .values_list("account_id", "data__report_id"))
    void = _void_report_ids({rid for _, rid in recent})
    used = Counter(acc for acc, rid in recent if rid not in void)
    sent = 0
    for account in recipients:
        if used.get(account.pk, 0) >= DAILY_CAP:
            continue
        notify(account, ALERT_TYPE, title=title, body=body, data=data)
        sent += 1
    return sent
