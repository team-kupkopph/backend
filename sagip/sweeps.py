"""US-F0/E1/E2 · the scheduled sweeps — plain, unit-testable functions (mirrors US-S6:
"the rule is the deliverable, the schedule is a trigger"). A recurring trigger
(`manage.py run_sweeps`, invoked by cron — see US-F0's decision to record: cron over
Celery-beat, no new infra, revisit when Sprint 5 needs workers anyway) calls these; the
functions themselves have no opinion on when they run. Both are idempotent, and both stop
touching a report the instant it is claimed (E1) or resolved (E2).
"""
from django.db import transaction
from django.utils import timezone

from accounts.models import Account
from notifications.service import notify
from sagip.models import (
    CaseStatusHistory,
    OfferStatus,
    ReportOffer,
    RescueCase,
    StrayCondition,
    StrayReport,
    StrayStatus,
)
from sagip.status import set_report_status

# Condition -> hours a CLAIM may go without a status update before it's stalled (decision
# 14: injured/sick 6h · pregnant 12h · healthy 24h). US-E1's escalation cadence is
# DERIVED from this same table (below) rather than a second set of magic numbers —
# decision 14's "if either number moves, move both" applies here too.
CLAIM_WINDOW_HOURS = {
    StrayCondition.INJURED: 6,
    StrayCondition.SICK: 6,
    StrayCondition.PREGNANT: 12,
    StrayCondition.HEALTHY: 24,
}
_DEFAULT_WINDOW = CLAIM_WINDOW_HOURS[StrayCondition.HEALTHY]


def _claim_window_hours(condition):
    return CLAIM_WINDOW_HOURS.get(condition, _DEFAULT_WINDOW)


def _escalation_cadence_hours(condition):
    """Level-1 / level-2 ages (hours since reported) — 1/3 and 2/3 of that condition's
    claim window, so an unclaimed report widens its net well before a claim on it could
    ever go on to stall."""
    window = _claim_window_hours(condition)
    return (window / 3, window * 2 / 3)


def _level1_recipients(report):
    """'Widen to ~5km rescuers' (US-E1), approximated by CITY match. A person's location
    is city-only by design (decision 11 — no precise geom is stored for a person), so a
    literal radius query isn't answerable from this schema. A report with no resolved
    city has no honest scope to widen into: the level still advances (the record stays
    accurate either way), it just notifies no one."""
    if not report.city:
        return Account.objects.none()
    return Account.objects.filter(
        capabilities__capability="rescuer", capabilities__status="approved",
        addresses__city=report.city, addresses__is_primary=True,
    ).distinct()


def _level2_recipients():
    """Tier-2-eligible escalation partners (decision 4 / §3.5). Checks the
    `is_escalation_partner` flag, not the tier column directly — tier-1 can hold it too,
    by admin exception."""
    return Account.objects.filter(
        shelter_profile__is_escalation_partner=True,
        verifications__type="shelter_org", verifications__status="approved",
    ).distinct()


def escalate_reports(now=None):
    """US-E1 · widen the net on unclaimed reports. Only `reported` rows are ever
    considered — claiming (or resolving) a report removes it from the very next pass.
    Idempotent: the level is stored, so a level already reached is never re-fired; a
    sweep gap that lets a report cross both thresholds at once still fires exactly one
    notification per level, never a duplicate."""
    now = now or timezone.now()
    touched = []
    reports = StrayReport.objects.filter(status=StrayStatus.REPORTED, escalation_level__lt=2)
    for report in reports:
        level1_at, level2_at = _escalation_cadence_hours(report.condition)
        age_hours = (now - report.created_at).total_seconds() / 3600
        moved = False

        if report.escalation_level < 1 and age_hours >= level1_at:
            report.escalation_level = 1
            report.save(update_fields=["escalation_level"])
            for acc in _level1_recipients(report):
                notify(acc, "report_escalated", title="A stray nearby needs a rescuer",
                      body=f"A {report.get_condition_display().lower()} "
                           f"{report.get_species_display().lower()} in {report.city} "
                           f"still needs someone to claim it.",
                      data={"report_id": str(report.pk), "escalation_level": 1})
            moved = True

        if report.escalation_level < 2 and age_hours >= level2_at:
            report.escalation_level = 2
            report.save(update_fields=["escalation_level"])
            for acc in _level2_recipients():
                notify(acc, "report_escalated", title="An unclaimed stray needs a partner",
                      body=f"A {report.get_condition_display().lower()} "
                           f"{report.get_species_display().lower()} has gone unclaimed "
                           f"and could use your organization's reach.",
                      data={"report_id": str(report.pk), "escalation_level": 2})
            moved = True

        if moved:
            touched.append(report)
    return touched


def expire_stalled_claims(now=None):
    """US-E2 · a claim whose latest `case_status_history` row is older than its report's
    condition window reverts the report to `reported` — no user-facing release, the
    system simply recognizing the claimer never showed. The `RescueCase` row is KEPT
    (re-claimable; `expired_at` makes lapses countable per claimer). Every account that
    ever offered on the report — matched or not — is notified `case_reopened`; a MATCHED
    offer whose own 48h window hasn't separately lapsed reverts to OPEN, since the claim
    it was matched to just failed and that support is genuinely available again."""
    now = now or timezone.now()
    expired = []
    active = (RescueCase.objects.filter(expired_at__isnull=True)
              .exclude(report__status=StrayStatus.RESOLVED)
              .select_related("report"))
    for case in active:
        latest = (CaseStatusHistory.objects.filter(report=case.report)
                  .order_by("-changed_at").first())
        # A claim always writes at least one history row (US-K1); this fallback only
        # matters for a case seeded directly in the DB with no history at all.
        anchor = latest.changed_at if latest else case.claimed_at
        window = _claim_window_hours(case.report.condition)
        stalled_hours = (now - anchor).total_seconds() / 3600
        if stalled_hours < window:
            continue

        report = case.report
        with transaction.atomic():
            case.expired_at = now
            case.save(update_fields=["expired_at"])
            set_report_status(report, StrayStatus.REPORTED, None,
                              note="Auto-expired: no update within the claim window")

            offerers = list(Account.objects.filter(report_offers__report=report).distinct())
            for offer in ReportOffer.objects.filter(report=report, status=OfferStatus.MATCHED):
                offer.status = OfferStatus.OPEN if offer.expires_at > now else OfferStatus.EXPIRED
                offer.save(update_fields=["status"])
            for acc in offerers:
                notify(acc, "case_reopened", title="A case you offered to help with reopened",
                      body=f"The {report.get_species_display().lower()} in "
                           f"{report.city or 'the area'} needs help again.",
                      data={"report_id": str(report.pk)})
        expired.append(case)
    return expired


def expire_offers(now=None):
    """US-N2 · an `open` offer past its 48h window (decision 14) moves to `expired`.

    Nothing else wrote this transition before this sweep existed — US-E2's own
    "a reopened case still has people to re-ask" arithmetic (above) silently assumed
    `expired` offers would already be filtered out by the time it ran, but the value was
    never actually written anywhere. Idempotent (only `OPEN` rows are touched) and never
    touches `MATCHED` — a matched offer's fate is `expire_stalled_claims`'s call, not
    this sweep's; this one only ever moves `open → expired`.
    """
    now = now or timezone.now()
    offers = ReportOffer.objects.filter(status=OfferStatus.OPEN, expires_at__lte=now)
    count = offers.update(status=OfferStatus.EXPIRED)
    return count
