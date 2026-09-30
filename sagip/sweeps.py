"""US-F0/E1/E2 · the scheduled sweeps — plain, unit-testable functions (mirrors US-S6:
"the rule is the deliverable, the schedule is a trigger"). A recurring trigger
(`manage.py run_sweeps`, invoked by cron — see US-F0's decision to record: cron over
Celery-beat, no new infra, revisit when Sprint 5 needs workers anyway) calls these; the
functions themselves have no opinion on when they run. Both are idempotent, and both stop
touching a report the instant it is claimed (E1) or moves past `claimed` (E2).
"""
import math

from django.db import transaction
from django.utils import timezone

from accounts.models import Account
from notifications.models import Notification
from notifications.service import notify
from sagip import notices
from sagip.alerts import alert_on_reopen, already_alerted_ids, verified_in_city
from sagip.geo import centroid_for, distance_km
from sagip.models import (
    CaseStatusHistory,
    OfferStatus,
    ReportOffer,
    ReportType,
    RescueCase,
    StrayCondition,
    StrayReport,
    StrayStatus,
)
from sagip.notices import with_article
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

# S5 · how far level 2 reaches: a partner is paged when its city's centre is within this
# distance of the report's own point. Wide enough to cross into neighbouring Metro Manila
# cities (level 1 already covered the report's own city), narrow enough that a Cebu partner
# is never paged for a Marikina dog. A policy number — move it deliberately.
LEVEL2_RADIUS_KM = 15


def _claim_window_hours(condition):
    return CLAIM_WINDOW_HOURS.get(condition, _DEFAULT_WINDOW)


# S9 · the claimer is warned once this share of the window has passed (4.5 h of an injured
# animal's 6). On the hourly tick that lands between 75% and 75% + 1 h — still before expiry
# for every window in CLAIM_WINDOW_HOURS.
WARN_AT_FRACTION = 0.75


def _claim_anchor(case):
    """The moment a claim's clock started: its latest status row (a claim always writes one,
    US-K1), falling back to `claimed_at` for a case seeded with no history."""
    latest = (CaseStatusHistory.objects.filter(report=case.report)
              .order_by("-changed_at").first())
    return latest.changed_at if latest else case.claimed_at


def claim_due_at(case):
    """S9 · when this claim lapses if nothing is posted, or None when it can't lapse — it
    already did, or the animal is in custody (S1: only a `claimed` case can stall). The API
    shows this and the sweeps act on it, so both read the same rule."""
    if case.expired_at is not None or case.report.status != StrayStatus.CLAIMED:
        return None
    return _claim_anchor(case) + timezone.timedelta(
        hours=_claim_window_hours(case.report.condition))


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
    accurate either way), it just notifies no one.

    Since D2 the audience is sagip.alerts.verified_in_city — verified rescuers AND verified
    shelters (S16: a shelter in the report's own city used to be skipped until level 2), active
    accounts only, city spelling tolerated (S4). It skips the reporter, and anyone the
    report-time alert already asked about this same report: level 1 is for the people that
    alert didn't reach (a healthy stray, a daily cap, someone verified since)."""
    return (verified_in_city(report.city)
            .exclude(pk=report.reporter_account_id)
            .exclude(pk__in=already_alerted_ids(report)))


def _level2_recipients(report):
    """Tier-2-eligible escalation partners (decision 4 / §3.5) NEAR the report. Checks the
    `is_escalation_partner` flag, not the tier column directly — tier-1 can hold it too,
    by admin exception.

    ⚠️ Near, not everywhere (S5). This used to page every partner in the country for every
    report. A partner's location is its primary address's city (a shelter has no stored
    point), placed by that city's centre; one that can't be placed isn't paged, because
    there is no honest way to call it nearby. Partners are few, so this filters in Python."""
    partners = (Account.objects.filter(
        shelter_profile__is_escalation_partner=True,
        verifications__type="shelter_org", verifications__status="approved",
    ).distinct().prefetch_related("addresses"))
    near = []
    for acc in partners:
        primary = next((a for a in acc.addresses.all() if a.is_primary), None)
        centre = centroid_for(primary.city) if primary else None
        if centre and distance_km(report.geom.y, report.geom.x, *centre) <= LEVEL2_RADIUS_KM:
            near.append(acc)
    return near


def _condition_species(report):
    return f"{report.get_condition_display().lower()} {report.get_species_display().lower()}"


def escalate_reports(now=None):
    """US-E1 · widen the net on unclaimed reports. Only `reported` rows are ever
    considered — claiming (or resolving) a report removes it from the very next pass.
    Idempotent: the level is stored, so a level already reached is never re-fired; a
    sweep gap that lets a report cross both thresholds at once still fires exactly one
    notification per level, never a duplicate."""
    now = now or timezone.now()
    touched = []
    # D6 · a lost pet is its owner's to find and nobody's to claim — it never pages rescuers.
    reports = (StrayReport.objects.filter(status=StrayStatus.REPORTED, escalation_level__lt=2)
               .exclude(report_type=ReportType.LOST))
    for report in reports:
        level1_at, level2_at = _escalation_cadence_hours(report.condition)
        age_hours = (now - report.created_at).total_seconds() / 3600
        moved = False

        if report.escalation_level < 1 and age_hours >= level1_at:
            report.escalation_level = 1
            report.save(update_fields=["escalation_level"])
            for acc in _level1_recipients(report):
                notify(acc, "report_escalated", title="A stray nearby needs a rescuer",
                      body=f"{with_article(_condition_species(report))} in {report.city} "
                           f"still needs someone to claim it.",
                      data={"report_id": str(report.pk), "escalation_level": 1})
            moved = True

        if report.escalation_level < 2 and age_hours >= level2_at:
            report.escalation_level = 2
            report.save(update_fields=["escalation_level"])
            for acc in _level2_recipients(report):
                notify(acc, "report_escalated", title="An unclaimed stray needs a partner",
                      body=f"{with_article(_condition_species(report))} has gone unclaimed "
                           f"and could use your organization's reach.",
                      data={"report_id": str(report.pk), "escalation_level": 2})
            moved = True

        if moved:
            touched.append(report)
    return touched


def _is_stalled(case, now):
    return ((now - _claim_anchor(case)).total_seconds() / 3600
            >= _claim_window_hours(case.report.condition))


def _stalled_case_ids(now):
    """The scan: claims that LOOK stalled. Unlocked and possibly stale by the time it's acted on —
    `_expire_case` decides for real."""
    active = (RescueCase.objects.filter(expired_at__isnull=True,
                                        report__status=StrayStatus.CLAIMED)
              .select_related("report"))
    return [case.pk for case in active if _is_stalled(case, now)]


def _expire_case(case_id, now):
    """C10b · lapse ONE claim, deciding under lock. Case, then report (the order the status and
    release views use). Returns the lapsed case, or None when it moved on, was released, or is no
    longer stalled."""
    with transaction.atomic():
        case = (RescueCase.objects.select_for_update()
                .filter(pk=case_id, expired_at__isnull=True).first())
        if case is None:
            return None
        report = StrayReport.objects.select_for_update().get(pk=case.report_id)
        if report.status != StrayStatus.CLAIMED:
            return None
        case.report = report
        if not _is_stalled(case, now):
            return None
        reopen_case(case, None, "Auto-expired: no update within the claim window", now)
        # S9 · inside the transaction, so a rolled-back expiry tells no one.
        notices.claim_lapsed(case, _claim_window_hours(report.condition))
        return case


def expire_stalled_claims(now=None):
    """US-E2 · a claim whose latest `case_status_history` row is older than its report's
    condition window reverts the report to `reported` — no user-facing release, the
    system simply recognizing the claimer never showed. The `RescueCase` row is KEPT
    (re-claimable; `expired_at` makes lapses countable per claimer). Every account that
    ever offered on the report — matched or not — is notified `case_reopened`; a MATCHED
    offer whose own 48h window hasn't separately lapsed reverts to OPEN, since the claim
    it was matched to just failed and that support is genuinely available again.

    ⚠️ Only `claimed` cases can stall. Expiry answers "the claimer never showed up"; once the
    report is `rescued` or `safe` the animal is in someone's custody, and reverting it would
    put an animal in a rescuer's home back on the public map — re-claimable, with the exact
    spot handed to the next claimer (S1, dev/sagip-build-review.md). This used to exclude
    only `resolved`, which let a `safe` case lapse after its condition window.

    C10b · the scan is unlocked; each lapse is decided under a row lock by `_expire_case`, so a
    status update or a release that lands between the scan and the lapse wins."""
    now = now or timezone.now()
    lapsed = (_expire_case(case_id, now) for case_id in _stalled_case_ids(now))
    return [case for case in lapsed if case is not None]


@transaction.atomic
def reopen_case(case, by, note, now=None):
    """Put a claimed report back on the map — shared by the stalled-claim sweep (US-E2) and a
    claimer's own release (D3), so both reopen a report the same way.

    The `RescueCase` row is KEPT with `expired_at` set, so the report can be re-claimed at once
    and the lapse stays countable per claimer (a release counts like a lapse, without penalty).
    A MATCHED offer whose own 48 h window hasn't separately lapsed reverts to OPEN — the claim it
    was matched to just ended and that help is available again — and every account that ever
    offered is told. Telling the claimer and the reporter is the CALLER's job: a lapse and a
    release say different things to them."""
    now = now or timezone.now()
    report = case.report
    case.expired_at = now
    case.save(update_fields=["expired_at"])
    set_report_status(report, StrayStatus.REPORTED, by, note=note)
    offerers = list(Account.objects.filter(report_offers__report=report).distinct())
    for offer in ReportOffer.objects.filter(report=report, status=OfferStatus.MATCHED):
        offer.status = OfferStatus.OPEN if offer.expires_at > now else OfferStatus.EXPIRED
        offer.save(update_fields=["status"])
    for acc in offerers:
        notify(acc, "case_reopened", title="A case you offered to help with reopened",
               body=f"The {report.get_species_display().lower()} in "
                    f"{report.city or 'the area'} needs help again.",
               data={"report_id": str(report.pk)})
    # Re-alert · the people who haven't been asked about this report yet. Best-effort: a paging
    # failure must not undo the reopen (the report is back on the map either way).
    try:
        with transaction.atomic():
            alert_on_reopen(report, released_by=case.claimed_by_account_id, now=now)
    except Exception:
        import logging
        logging.getLogger("kupkop.alerts").exception("re-alert on reopen failed")


def warn_due_claims(now=None):
    """S9 · warn a claimer once WARN_AT_FRACTION of their window has passed with no update,
    while there is still time to post one. Idempotent: one `claim_due` per case, ever — a
    claim that moves on to `rescued` leaves this sweep's scope, and a re-claim after expiry
    is a new case with its own warning."""
    now = now or timezone.now()
    warned = []
    active = (RescueCase.objects.filter(expired_at__isnull=True,
                                        report__status=StrayStatus.CLAIMED)
              .select_related("report", "claimed_by_account"))
    for case in active:
        window = _claim_window_hours(case.report.condition)
        elapsed = (now - _claim_anchor(case)).total_seconds() / 3600
        if not (window * WARN_AT_FRACTION <= elapsed < window):
            continue
        if Notification.objects.filter(account=case.claimed_by_account, type="claim_due",
                                       data__case_id=str(case.pk)).exists():
            continue
        notices.claim_due(case, max(1, math.ceil(window - elapsed)))
        warned.append(case)
    return warned


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
