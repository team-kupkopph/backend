"""US-L2 · lost<->found matching (Tech Spec §11).

Transparent, explainable weighted heuristics — no ML (photo similarity is Phase 2+, §11.2).
Cheap indexed filters first (opposite type, same species, 10 km, ±30 d, open only), then a
[0,1] score per candidate; persist the strong ones as `suggested` for a human to confirm.
Matching NEVER auto-resolves a case (§11, first sentence).

Weights (§11.2) are settings so thresholds tune without a code change (§11.4).
"""
from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from django.utils import timezone

from common.analytics import emit
from notifications.service import notify

from .models import MatchStatus, ReportMatch, ReportType, StrayReport, StrayStatus

RADIUS_KM = getattr(settings, "MATCH_RADIUS_KM", 10)
WINDOW_DAYS = getattr(settings, "MATCH_WINDOW_DAYS", 30)
THRESHOLD = getattr(settings, "MATCH_THRESHOLD", 0.45)
TOP_N = getattr(settings, "MATCH_TOP_N", 5)

W_GEO, W_TIME, W_BREED, W_COLOR, W_SIZESEX = 0.35, 0.20, 0.20, 0.15, 0.10

_OPPOSITE = {ReportType.LOST: ReportType.FOUND, ReportType.FOUND: ReportType.LOST}


def _tokens(*vals):
    out = set()
    for v in vals:
        if v:
            out.update(w for w in v.lower().replace(",", " ").split() if w)
    return out


def _overlap(a, b):
    """Jaccard token overlap in [0,1]; 0 when either side has no tokens (missing -> 0 term)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _sizesex(r1, r2):
    parts = []
    if r1.size_category and r2.size_category:
        parts.append(1.0 if r1.size_category == r2.size_category else 0.0)
    if r1.sex and r2.sex:
        parts.append(1.0 if r1.sex == r2.sex else 0.0)
    return sum(parts) / len(parts) if parts else 0.0


def score_signals(report, candidate, distance_m):
    """Per-signal sub-scores in [0,1] — returned for explainability so the UI can say WHY
    ('400 m away · 2 days apart · colors match') rather than a bare percentage."""
    geo = max(0.0, 1 - (distance_m / (RADIUS_KM * 1000)))
    days_apart = abs((report.created_at - candidate.created_at).days)
    time = max(0.0, 1 - (days_apart / WINDOW_DAYS))
    breed = _overlap(_tokens(report.breed, report.notes),
                     _tokens(candidate.breed, candidate.notes))
    color = _overlap(_tokens(report.color_markings), _tokens(candidate.color_markings))
    sizesex = _sizesex(report, candidate)
    return {"geo": round(geo, 3), "time": round(time, 3), "breed": round(breed, 3),
            "color": round(color, 3), "size_sex": round(sizesex, 3)}


def total_score(signals):
    return (W_GEO * signals["geo"] + W_TIME * signals["time"] + W_BREED * signals["breed"]
            + W_COLOR * signals["color"] + W_SIZESEX * signals["size_sex"])


def _candidates(report):
    opposite = _OPPOSITE.get(report.report_type)
    if opposite is None:            # a plain stray sighting doesn't participate in L&F matching
        return StrayReport.objects.none()
    window_lo = report.created_at - timezone.timedelta(days=WINDOW_DAYS)
    window_hi = report.created_at + timezone.timedelta(days=WINDOW_DAYS)
    return (StrayReport.objects
            .filter(report_type=opposite, species=report.species,
                    geom__dwithin=(report.geom, D(km=RADIUS_KM)),
                    created_at__gte=window_lo, created_at__lte=window_hi)
            .exclude(status=StrayStatus.RESOLVED)
            .exclude(pk=report.pk)
            .annotate(_distance=Distance("geom", report.geom)))


def run_matching(report):
    """Score `report` against candidate opposite-type reports, persist the top-N strong ones
    as `suggested`, and notify both reporters of each NEWLY suggested pair. Returns the list of
    persisted ReportMatch rows (with their signals attached as `.signals`). Idempotent: a
    re-run refreshes scores in place, never re-notifies, and never touches a decided pair."""
    scored = []
    for cand in _candidates(report):
        signals = score_signals(report, cand, cand._distance.m)
        total = total_score(signals)
        if total >= THRESHOLD:
            scored.append((total, signals, cand))
    scored.sort(key=lambda t: t[0], reverse=True)

    persisted = []
    for total, signals, cand in scored[:TOP_N]:
        match, created = ReportMatch.objects.get_or_create(
            report=report, matched_report=cand,
            defaults={"score": round(total, 3), "status": MatchStatus.SUGGESTED})
        if not created and match.status == MatchStatus.SUGGESTED:
            match.score = round(total, 3)          # refresh a still-open suggestion
            match.save(update_fields=["score"])
        match.signals = signals
        persisted.append(match)
        if created:
            # Notify only on first suggestion of a pair — the row's existence IS the dedup, so
            # a re-scored suggestion never re-buzzes (derived-not-stored, the sweep-reminder rule).
            _notify_pair(report, cand)
            emit("match_suggested", score_bucket=_bucket(total))
    return persisted


def _bucket(score):
    return "high" if score >= 0.8 else "med" if score >= 0.6 else "low"


def sweep_matches():
    """§11.4 · nightly safety net — re-run matching for every still-open lost/found report
    inside the time window, catching near-miss pairs and newly-added detail. Idempotent (new
    pairs only notify once). Returns the list of matches persisted this run."""
    cutoff = timezone.now() - timezone.timedelta(days=WINDOW_DAYS)
    reports = (StrayReport.objects
               .filter(report_type__in=(ReportType.LOST, ReportType.FOUND),
                       created_at__gte=cutoff)
               .exclude(status=StrayStatus.RESOLVED))
    persisted = []
    for report in reports:
        persisted.extend(run_matching(report))
    return persisted


def _notify_pair(report, candidate):
    for who, own in ((report.reporter_account, report), (candidate.reporter_account, candidate)):
        if who is None:                            # anonymous report — nobody to notify
            continue
        notify(who, "match_suggested",
               title="Possible match",
               body="We found a possible match for your lost & found report.",
               data={"report_id": str(own.pk)})
