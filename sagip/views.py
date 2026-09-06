from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.analytics import emit
from common.throttles import OfferCreateThrottle, ReportCreateThrottle
from listings.permissions import IsVerifiedRescuer
from notifications.service import notify
from sagip.geo import centroid_for, coarsen_point
from sagip.models import (
    MatchStatus,
    OfferStatus,
    ReportMatch,
    ReportOffer,
    ReportType,
    RescueCase,
    StrayReport,
    StrayReportPhoto,
    StrayStatus,
)
from sagip.permissions import is_active_claimer
from sagip.serializers import CaseStatusUpdateSerializer, OfferCreateSerializer, ReportCreateSerializer
from sagip.status import set_report_status

DEFAULT_RADIUS_KM = 10.0
# decision 14: offers must outlive the longest claim window (24h) so a reopened case
# still has people to re-ask — if either number moves, move both.
OFFER_WINDOW_HOURS = 48

# US-K2 · a case can only move forward through this order; `resolved` is terminal.
# `claimed` is included as the baseline so a target's index can be compared against
# whatever the report is currently at (never itself a valid POST target — see the
# serializer's choices).
CASE_STATUS_ORDER = {
    StrayStatus.CLAIMED: 0,
    StrayStatus.RESCUED: 1,
    StrayStatus.SAFE: 2,
    StrayStatus.RESOLVED: 3,
}


def _already_claimed():
    return Response({"error": {"code": "already_claimed",
                               "message": "This report already has an active claim"}},
                    status=409)


class ReportsCreateView(APIView):
    """US-S1 · report a stray. A guest tapping this gets 401 (the client raises the signup
    wall and resumes the report after signup). `is_anonymous` hides the reporter from other
    users, but the row still records `reporter_account_id`."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [ReportCreateThrottle]  # US-SEC2 · per-account, 20/day

    def post(self, request):
        s = ReportCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        # US-O3 · exactly-once for the offline outbox (§13.3). A queued report is retried
        # until it lands, and the server cannot tell a replay from a second animal by looking
        # at the fields — two reports of the same dog, minutes apart, are legitimately
        # identical. Returning the EXISTING row (rather than creating a second) is what stops
        # a flaky connection from dispatching two rescuers to one animal.
        #
        # The lookup is scoped to the caller, so a guessed or replayed key can never hand
        # someone another person's report — precise coordinates included (§12.5).
        #
        # Returned BEFORE the create block, so the replay also skips every side effect:
        # no second matcher run (US-L2), no duplicate reunion push, no repeated analytics.
        idem = d.get("idempotency_key")
        if idem:
            existing = StrayReport.objects.filter(
                reporter_account=request.user, idempotency_key=idem).first()
            if existing is not None:
                return Response({"report_id": str(existing.report_id),
                                 "status": existing.status}, status=200)
        # US-L1 · describable fields (lost/found). A pet_id the caller owns prefills any the
        # caller left blank — but the values are STORED on the report (D-S6-1), so a later pet
        # edit can't rewrite what was reported. Ownership-checked: another user's pet is ignored.
        describables = {k: (d.get(k) or None) for k in ("breed", "color_markings",
                                                        "size_category", "sex")}
        pet_id = d.get("pet_id")
        if pet_id is not None:
            from listings.models import Pet
            pet = Pet.objects.filter(pk=pet_id, owner_account=request.user).first()
            if pet is not None:
                for field in ("breed", "color_markings", "size_category", "sex"):
                    if not describables[field]:
                        describables[field] = getattr(pet, field, None) or None
        with transaction.atomic():
            report = StrayReport.objects.create(
                reporter_account=request.user,
                report_type=d.get("report_type", "stray"),
                pet_id=pet_id,
                is_anonymous=d.get("is_anonymous", False),
                species=d["species"], condition=d["condition"], notes=d.get("notes", ""),
                geom=Point(d["lng"], d["lat"], srid=4326),   # PostGIS: (x=lng, y=lat)
                location_text=d.get("location_text", ""),
                city=(d.get("city") or "").strip() or None,   # client-resolved city label, or NULL
                status="reported", escalation_level=0,
                idempotency_key=idem or None, **describables)
            for photo in d.get("photos", []):
                StrayReportPhoto.objects.create(report=report, url=photo["file_url"])
        # US-L2 · a new lost/found report triggers matching (§11). Best-effort: a matcher
        # failure must never break a welfare report submission. Inert for plain strays.
        if report.report_type in (ReportType.LOST, ReportType.FOUND):
            import logging

            from sagip.matching import run_matching
            try:
                run_matching(report)
            except Exception:
                logging.getLogger("kupkop.match").exception("matching failed on report create")
        emit("report_created", type=report.report_type, species=report.species)
        return Response({"report_id": str(report.report_id), "status": "reported"}, status=201)


class ReportClaimView(APIView):
    """US-K1 · claim an unclaimed report — exclusive and binding (decision 11). Only an
    approved rescuer capability (Verified Member) or an approved shelter_org verification
    (verified shelter) may claim (IsVerifiedRescuer); an unverified caller gets 403.

    Creating the case, moving the report to `claimed` (via `set_report_status`, so it's
    logged), matching every open offer on the report, and notifying the reporter + each
    matched offerer all happen inside one transaction — a claim that partially lands
    (case created but offers left dangling, or vice versa) is worse than no claim.

    Exclusivity is enforced twice: a `select_for_update` existence check inside the
    transaction (serializes concurrent requests on the same report and returns a clean
    409), backed by the partial-unique DB constraint (`idx_rescue_case_active`) as the
    real safety net — an `IntegrityError` that slips past the check still becomes a 409,
    never a 500."""
    permission_classes = [IsVerifiedRescuer]

    def post(self, request, report_id):
        try:
            with transaction.atomic():
                report = (StrayReport.objects.select_for_update()
                          .filter(pk=report_id).first())
                if report is None:
                    return Response({"error": {"code": "not_found",
                                               "message": "No such report"}}, status=404)
                if RescueCase.objects.filter(report=report, expired_at__isnull=True).exists():
                    return _already_claimed()

                case = RescueCase.objects.create(report=report, claimed_by_account=request.user)
                set_report_status(report, StrayStatus.CLAIMED, request.user)

                offers = ReportOffer.objects.select_for_update().filter(
                    report=report, status=OfferStatus.OPEN)
                for offer in offers:
                    offer.status = OfferStatus.MATCHED
                    offer.save(update_fields=["status"])
                    notify(offer.account, "offer_matched",
                          title="Your offer was matched",
                          body=f"Someone claimed the {report.get_species_display().lower()} "
                               f"you offered to help.",
                          data={"report_id": str(report.pk), "case_id": str(case.pk)})

                # The reporter is always notified — is_anonymous hides them from OTHER
                # users, never from their own report (rule 6).
                if report.reporter_account_id:
                    notify(report.reporter_account, "report_claimed",
                          title="Your report was claimed",
                          body=f"{request.user.display_name} is on the way.",
                          data={"report_id": str(report.pk), "case_id": str(case.pk)})
        except IntegrityError:
            return _already_claimed()

        # US-SEC1 · the claimer's need for the exact spot is the reason the app's one
        # GPS exception exists at all — hand it over the moment the claim lands.
        return Response({"case_id": str(case.pk), "status": "claimed",
                         "precise_location": {"lat": report.geom.y, "lng": report.geom.x}},
                        status=201)


class CaseStatusView(APIView):
    """US-K2 · work a claimed case toward safe/resolved. Only the claimer may move it —
    an ownership check, not a role gate, since claiming already proved verification.
    Forward-only through CASE_STATUS_ORDER (never backward, never re-posting the current
    status); `resolved` is terminal. Every move goes through `set_report_status`, so it's
    what keeps a worked case from looking stalled to US-E2's auto-expiry sweep."""
    permission_classes = [IsAuthenticated]

    def post(self, request, case_id):
        case = RescueCase.objects.select_related("report").filter(pk=case_id).first()
        if case is None:
            return Response({"error": {"code": "not_found", "message": "No such case"}},
                            status=404)
        if case.claimed_by_account_id != request.user.pk:
            return Response({"error": {"code": "not_your_case",
                                       "message": "Only the claimer can update this case"}},
                            status=403)
        if case.expired_at is not None:
            return Response({"error": {"code": "case_expired",
                                       "message": "This claim has lapsed"}}, status=409)

        s = CaseStatusUpdateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        target = s.validated_data["status"]

        report = case.report
        current_rank = CASE_STATUS_ORDER.get(report.status, -1)
        if report.status == StrayStatus.RESOLVED:
            return Response({"error": {"code": "case_resolved",
                                       "message": "This case is already resolved"}}, status=409)
        if CASE_STATUS_ORDER[target] <= current_rank:
            return Response({"error": {"code": "not_forward",
                                       "message": "A case can only move forward"}}, status=409)

        with transaction.atomic():
            set_report_status(report, target, request.user, note=s.validated_data.get("note", ""))
            if target == StrayStatus.RESOLVED:
                if "outcome_notes" in s.validated_data:
                    case.outcome_notes = s.validated_data["outcome_notes"]
                if "outcome_photo_url" in s.validated_data:
                    case.outcome_photo_url = s.validated_data["outcome_photo_url"]
                case.resolved_at = timezone.now()
                case.save(update_fields=["outcome_notes", "outcome_photo_url", "resolved_at"])

        if target == StrayStatus.RESOLVED:
            # US-B1 · a resolved rescue can earn a badge (idempotent + reconciled nightly).
            from community.badges import award_badges_for
            award_badges_for(case.claimed_by_account)
            emit("case_resolved")

        return Response({"status": target}, status=200)


class CaseDetailView(APIView):
    """US-SEC1 · a claimer's own case, including the precise spot. Claim-time already
    hands over `precise_location` once; this is how the claimer gets it again later
    (re-opening the app, a different device) without it ever being embedded anywhere a
    non-claimer could read it. Only the active claimer may view — not even the reporter,
    who already gets it via `GET /reports/{id}`."""
    permission_classes = [IsAuthenticated]

    def get(self, request, case_id):
        case = RescueCase.objects.select_related("report").filter(pk=case_id).first()
        if case is None:
            return Response({"error": {"code": "not_found", "message": "No such case"}},
                            status=404)
        if case.claimed_by_account_id != request.user.pk:
            return Response({"error": {"code": "not_your_case",
                                       "message": "Only the claimer can view this case"}},
                            status=403)
        report = case.report
        approx_lat, approx_lng = coarsen_point(report.geom.y, report.geom.x)
        body = {
            "case_id": str(case.pk),
            "report": {
                "report_id": str(report.report_id), "species": report.species,
                "condition": report.condition, "city": report.city,
                "approx_location": {"lat": approx_lat, "lng": approx_lng},
            },
            "status": report.status,
            "claimed_at": case.claimed_at.isoformat(),
            "expired_at": case.expired_at.isoformat() if case.expired_at else None,
        }
        if case.expired_at is None:
            body["report"]["precise_location"] = {"lat": report.geom.y, "lng": report.geom.x}
        return Response(body)


class MyRescuesView(APIView):
    """US-K3 · the claimer's own cases — active, resolved, and expired — newest claim
    first. This is the claimer's home for the rescue loop."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (RescueCase.objects.filter(claimed_by_account=request.user)
              .select_related("report").order_by("-claimed_at"))
        cases = [{
            "case_id": str(c.pk),
            "report": {"report_id": str(c.report_id), "species": c.report.species,
                      "condition": c.report.condition, "city": c.report.city},
            "status": c.report.status,
            "claimed_at": c.claimed_at.isoformat(),
            "expired_at": c.expired_at.isoformat() if c.expired_at else None,
        } for c in qs]
        return Response({"cases": cases})


class ReportOffersView(APIView):
    """US-O1 · offer help on an unclaimed report — a non-exclusive commitment (decision
    12). Anyone signed in may offer (no verification gate — that's the point of the
    ladder's lower rung); allowed only while the report is `reported`, since a claimed or
    resolved case has nothing left to offer on. **Never moves `stray_report.status`** —
    an offer answers a different question than a claim does."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [OfferCreateThrottle]  # US-SEC2 · per-account, 20/hour

    def post(self, request, report_id):
        report = StrayReport.objects.filter(pk=report_id).first()
        if report is None:
            return Response({"error": {"code": "not_found", "message": "No such report"}},
                            status=404)
        if report.status != StrayStatus.REPORTED:
            return Response({"error": {"code": "report_not_open",
                                       "message": "This report is no longer open for offers"}},
                            status=409)

        s = OfferCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        offer_type = s.validated_data["offer_type"]
        if ReportOffer.objects.filter(report=report, account=request.user,
                                      offer_type=offer_type).exists():
            return Response({"error": {"code": "already_offered",
                                       "message": "You already offered this"}}, status=409)

        expires_at = timezone.now() + timezone.timedelta(hours=OFFER_WINDOW_HOURS)
        try:
            offer = ReportOffer.objects.create(
                report=report, account=request.user, offer_type=offer_type,
                status=OfferStatus.OPEN, expires_at=expires_at)
        except IntegrityError:
            # Backstop for the UNIQUE(report, account, offer_type) constraint, same
            # belt-and-suspenders shape as the claim's IntegrityError handling.
            return Response({"error": {"code": "already_offered",
                                       "message": "You already offered this"}}, status=409)

        if report.reporter_account_id:
            notify(report.reporter_account, "offer_received",
                  title="Someone offered to help",
                  body=f"{request.user.display_name} can help with "
                       f"{offer.get_offer_type_display().lower()}.",
                  data={"report_id": str(report.pk), "offer_id": str(offer.pk)})

        return Response({"offer_id": str(offer.pk), "status": offer.status,
                         "expires_at": offer.expires_at.isoformat()}, status=201)


class ReportOfferWithdrawView(APIView):
    """US-O2 · withdraw an offer — allowed only while it's still `open`. That
    reversibility is what earns offers the right to be low-effort (decision 12); once
    matched or expired there's nothing left to take back. No `withdrawn` status exists
    in the DDL's `offer_status` enum, so withdrawing deletes the row rather than
    recording a fourth state."""
    permission_classes = [IsAuthenticated]

    def delete(self, request, report_id, offer_id):
        offer = ReportOffer.objects.filter(pk=offer_id, report_id=report_id).first()
        if offer is None:
            return Response({"error": {"code": "not_found", "message": "No such offer"}},
                            status=404)
        if offer.account_id != request.user.pk:
            return Response({"error": {"code": "not_your_offer",
                                       "message": "You can only withdraw your own offer"}},
                            status=403)
        if offer.status != OfferStatus.OPEN:
            return Response({"error": {"code": "not_withdrawable",
                                       "message": "This offer can no longer be withdrawn"}},
                            status=409)
        offer.delete()
        return Response(status=204)


class MyOffersView(APIView):
    """US-O2 · the caller's own offers, newest first — the client groups them into
    Open / Matched / Expired."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (ReportOffer.objects.filter(account=request.user)
              .select_related("report").order_by("-created_at"))
        offers = [{
            "offer_id": str(o.pk),
            "report": {"report_id": str(o.report_id), "species": o.report.species,
                      "condition": o.report.condition, "city": o.report.city},
            "offer_type": o.offer_type,
            "status": o.status,
            "expires_at": o.expires_at.isoformat(),
        } for o in qs]
        return Response({"offers": offers})


class RescueMapView(APIView):
    """US-S4 · the public rescue map. Anyone (signed in or not) sees strays near a chosen
    city — a PostGIS proximity query ordered by distance from the city centroid, within a
    radius. Replaces the Sprint-1 stub at the same route.

    ⚠️ City-level only (§12.5): the response never carries the precise `geom`. The exact
    spot is what US-S2 disclosed as the report's one precise-GPS surface; handing it to
    anonymous callers here would quietly undo that. The map places pins at city
    granularity, not the reporter's exact location."""
    permission_classes = [AllowAny]

    def get(self, request):
        centroid_ll = centroid_for(request.query_params.get("city"))
        if centroid_ll is None:
            return Response({"reports": []})  # the map is city-scoped; no known city, nothing to show
        lat, lng = centroid_ll
        centroid = Point(lng, lat, srid=4326)
        try:
            radius_km = float(request.query_params.get("radius_km") or DEFAULT_RADIUS_KM)
        except (TypeError, ValueError):
            radius_km = DEFAULT_RADIUS_KM
        qs = (StrayReport.objects
              .filter(geom__dwithin=(centroid, D(km=radius_km)))
              .annotate(_distance=Distance("geom", centroid))
              .order_by("_distance"))
        status = request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        city = request.query_params.get("city")
        reports = [{"report_id": str(r.report_id), "species": r.species,
                    "condition": r.condition, "status": r.status,
                    "city": r.city or city,   # coarse label; precise geom deliberately withheld
                    "reported_at": r.created_at.isoformat()}
                   for r in qs]
        return Response({"reports": reports})


class MyReportsView(APIView):
    """US-S3 · the reporter's own list, with live status. Their own reports, so the list
    is theirs to see — still city-level fields, no precise coordinate echoed back."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = request.user.stray_reports.order_by("-created_at")
        results = [{"report_id": str(r.report_id), "species": r.species,
                    "condition": r.condition, "status": r.status,
                    "city": r.city, "created_at": r.created_at.isoformat()} for r in qs]
        return Response({"results": results})


class ReportDetailView(APIView):
    """US-S5 · public report detail (the shared-link case) + US-O3 · the reporter's
    waiting view + US-SEC1 · the precise-location split.

    Field-authorization table for this one route (never assumed from a bearer token's
    mere presence — every tier below is checked by identity):
      - Public (anyone, including a guest): report_id, species, condition, status, notes,
        city, reported_at, photos, `approx_location` (coarsened — see sagip.geo.coarsen_point;
        deterministic ~500m grid, never the real point).
      - Reporter only: `escalation_level`, `offers_count`, `status_history` (US-O3).
      - Reporter OR the report's active claimer only: `precise_location` — the real point.
        A reporter always gets it (it's their own report); a claimer needs it to actually
        find the animal, which is the entire reason the app's one GPS exception exists
        (decision 11). An EXPIRED claim no longer qualifies — see
        `sagip.permissions.is_active_claimer`.
    """
    permission_classes = [AllowAny]

    def get(self, request, report_id):
        r = StrayReport.objects.filter(pk=report_id).first()
        if r is None:
            return Response({"error": {"code": "not_found", "message": "No such report"}},
                            status=404)
        approx_lat, approx_lng = coarsen_point(r.geom.y, r.geom.x)
        body = {
            "report_id": str(r.report_id), "report_type": r.report_type,
            "species": r.species, "condition": r.condition,
            "status": r.status, "notes": r.notes or None, "city": r.city,
            "reported_at": r.created_at.isoformat(),
            "photos": [p.url for p in r.photos.order_by("uploaded_at")],
            "approx_location": {"lat": approx_lat, "lng": approx_lng}}

        is_reporter = request.user.is_authenticated and request.user.pk == r.reporter_account_id
        if is_reporter:
            body["escalation_level"] = r.escalation_level
            body["offers_count"] = r.offers.count()
            body["status_history"] = [
                {"status": h.status, "changed_at": h.changed_at.isoformat()}
                for h in r.status_history.order_by("changed_at")]

        if is_reporter or is_active_claimer(r, request.user):
            body["precise_location"] = {"lat": r.geom.y, "lng": r.geom.x}

        return Response(body)


def _match_repr(match, viewer_report):
    """Present the OTHER report in the pair, plus the recomputed per-signal reasons (§11.2)
    so the UI can say WHY, not just a percentage. Signals aren't stored — recomputed on read."""
    from sagip.matching import score_signals
    other = match.matched_report if match.report_id == viewer_report.pk else match.report
    dist_m = viewer_report.geom.distance(other.geom) * 111195  # rough deg->m, display only
    try:
        signals = score_signals(viewer_report, other, dist_m)
    except Exception:
        signals = None
    return {"match_id": str(match.pk), "status": match.status,
            "score": float(match.score) if match.score is not None else None,
            "signals": signals,
            "report": {"report_id": str(other.pk), "report_type": other.report_type,
                       "species": other.species, "breed": other.breed,
                       "color_markings": other.color_markings, "city": other.city,
                       "created_at": other.created_at.isoformat()}}


class ReportMatchesView(APIView):
    """US-L2 · GET the suggested matches for a report the caller reported. Either direction of a
    pair is surfaced (report OR matched_report is this report)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, report_id):
        report = StrayReport.objects.filter(pk=report_id).first()
        if report is None:
            return Response({"error": {"code": "not_found", "message": "No such report"}},
                            status=404)
        if report.reporter_account_id != request.user.pk:
            return Response({"error": {"code": "forbidden",
                                       "message": "Not your report"}}, status=403)
        from django.db.models import Q
        matches = (ReportMatch.objects
                   .filter(Q(report=report) | Q(matched_report=report))
                   .exclude(status=MatchStatus.DISMISSED)
                   .select_related("report", "matched_report").order_by("-score"))
        return Response({"results": [_match_repr(m, report) for m in matches]})


class ReportMatchDecisionView(APIView):
    """US-L2 · confirm or dismiss a suggested match. Either reporter in the pair may decide
    (§11.3); a second decision on an already-decided match is 409 match_decided (the H3 TOCTOU
    posture). Confirm links the pair and moves BOTH reports toward resolved (a reunion)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, report_id, match_id, action):
        match = (ReportMatch.objects.select_related("report", "matched_report")
                 .filter(pk=match_id).first())
        if match is None or report_id not in (match.report_id, match.matched_report_id):
            return Response({"error": {"code": "not_found", "message": "No such match"}},
                            status=404)
        reporters = {match.report.reporter_account_id, match.matched_report.reporter_account_id}
        if request.user.pk not in reporters:
            return Response({"error": {"code": "forbidden",
                                       "message": "Not your match to decide"}}, status=403)
        with transaction.atomic():
            locked = ReportMatch.objects.select_for_update().get(pk=match.pk)
            if locked.status != MatchStatus.SUGGESTED:
                return Response({"error": {"code": "match_decided",
                                           "message": "This match was already decided"}},
                                status=409)
            if action == "confirm":
                locked.status = MatchStatus.CONFIRMED
                locked.save(update_fields=["status"])
                for rep in (match.report, match.matched_report):
                    if rep.status != StrayStatus.RESOLVED:
                        set_report_status(rep, StrayStatus.RESOLVED, request.user,
                                          note="lost & found match confirmed")
            else:
                locked.status = MatchStatus.DISMISSED
                locked.save(update_fields=["status"])
        emit("match_confirmed" if action == "confirm" else "match_dismissed")
        return Response({"status": locked.status})
