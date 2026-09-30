from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.analytics import emit
from common.throttles import OfferCreateThrottle, ReportCreateThrottle
from listings.permissions import IsVerifiedRescuer
from notifications.models import Notification
from notifications.service import notify
from sagip import alerts, contact, notices
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
from sagip.serializers import (
    CaseStatusUpdateSerializer,
    ClaimReleaseSerializer,
    ContactConsentSerializer,
    OfferCreateSerializer,
    ReportCloseSerializer,
    ReportCreateSerializer,
)
from sagip.status import set_report_status
from sagip.sweeps import claim_due_at, reopen_case

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


def _report_not_open():
    return Response({"error": {"code": "report_not_open",
                               "message": "This report is no longer open"}}, status=409)


def _iso(dt):
    return dt.isoformat() if dt else None


def _approx_location(report):
    """The public, coarsened point (US-SEC1 · a deterministic ~500 m grid) — never the real
    one. The only form of a report's position anyone but its reporter and claimer receives."""
    lat, lng = coarsen_point(report.geom.y, report.geom.x)
    return {"lat": lat, "lng": lng}


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
                contact_share_consent=d.get("contact_share_consent", False),
                contact_share_consent_at=(timezone.now() if d.get("contact_share_consent")
                                          else None),
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
        # D2 / S6 · page nearby verified rescuers + shelters about an urgent animal NOW, not two
        # hours from now. Best-effort, like the matcher: a push failure must never lose a
        # welfare report. After the idempotency return above, so an outbox replay pages no one.
        try:
            alerts.alert_at_report(report)
        except Exception:
            import logging
            logging.getLogger("kupkop.alerts").exception("report-time alert failed")
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
                # S21 · a report resolved WITHOUT a case — a confirmed lost & found match, or
                # (S11) its reporter closing it — has no active case, so the check above let it
                # be claimed straight back to `claimed`. After the case check so a lost race
                # still reads as "someone else got there first".
                if report.status != StrayStatus.REPORTED:
                    return _report_not_open()

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

        # S10 · the reporter hears every step; matched offerers hear the ending.
        notices.case_progress(report, case, target)

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
            "claim_due_at": _iso(claim_due_at(case)),
        }
        if case.expired_at is None:
            body["report"]["precise_location"] = {"lat": report.geom.y, "lng": report.geom.x}
            # S8 · the landmark is as precise as the pin, so it follows the pin.
            body["report"]["location_text"] = report.location_text or None
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
            "claim_due_at": _iso(claim_due_at(c)),   # S9 · null once it can't lapse
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
                status=OfferStatus.OPEN, expires_at=expires_at,
                note=s.validated_data.get("note", ""),
                contact_share_consent=s.validated_data["contact_share_consent"],
                contact_share_consent_at=(timezone.now()
                                          if s.validated_data["contact_share_consent"] else None))
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
            # ⚠️ Not a bare empty list (S15). "No reports" and "this city can't be searched"
            # used to be the same `{"reports": []}`, and the app told people in an uncovered
            # city "No strays reported near you — that's good news". `city_supported` lets the
            # client say the true thing; additive, so older clients are unaffected.
            return Response({"reports": [], "city_supported": False})
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
                    "reported_at": r.created_at.isoformat(),
                    # S14 · the same ~500 m grid point GET /reports/{id} already publishes,
                    # so the map can draw each report without exposing anything new.
                    "approx_location": _approx_location(r)}
                   for r in qs]
        return Response({"reports": reports, "city_supported": True})


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
      - Reporter only: `escalation_level`, `offers_count`, `status_history` (US-O3),
        `escalation_notified` (S5 · people each level actually reached).
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
        body = {
            "report_id": str(r.report_id), "report_type": r.report_type,
            "species": r.species, "condition": r.condition,
            "status": r.status, "notes": r.notes or None, "city": r.city,
            "reported_at": r.created_at.isoformat(),
            "photos": [p.url for p in r.photos.order_by("uploaded_at")],
            "approx_location": _approx_location(r)}

        is_reporter = request.user.is_authenticated and request.user.pk == r.reporter_account_id
        if is_reporter:
            body["escalation_level"] = r.escalation_level
            body["offers_count"] = r.offers.count()
            history = list(r.status_history.order_by("changed_at"))
            body["status_history"] = [
                {"status": h.status, "changed_at": h.changed_at.isoformat(), "note": h.note}
                for h in history]
            body["escalation_notified"] = _escalation_notified(r)
            # S10 · who has it, and how it ended. A lapsed claimer is not named: they are no
            # longer the one helping. Closed-by-reporter reports have no case, so no outcome.
            case = r.cases.filter(expired_at__isnull=True).select_related(
                "claimed_by_account").first()
            body["claimer"] = ({"display_name": case.claimed_by_account.display_name}
                               if case else None)
            body["outcome"] = ({"notes": case.outcome_notes or None,
                                "photo_url": case.outcome_photo_url or None,
                                "resolved_at": case.resolved_at.isoformat()}
                               if case and case.resolved_at else None)
            body["close_reason"] = _close_reason(r, history)
            body["contact_shared"] = r.contact_share_consent        # D1 · their own consent
            body["is_anonymous"] = r.is_anonymous                   # D8 · their own choice

        claimer = is_active_claimer(r, request.user)
        if is_reporter or claimer:
            body["precise_location"] = {"lat": r.geom.y, "lng": r.geom.x}
            # S8 · the landmark the reporter typed is as precise as the pin, so it follows
            # the pin's rule. It used to be stored and returned by no endpoint at all.
            body["location_text"] = r.location_text or None
        if claimer:
            # S27 · a claimer reading their report (from a push, or the map) can find the
            # case; S9 · and see when it lapses.
            mine = r.cases.get(claimed_by_account=request.user, expired_at__isnull=True)
            body["my_case"] = {"case_id": str(mine.pk), "claim_due_at": _iso(claim_due_at(mine)),
                               "contact_shared": mine.contact_share_consent}   # D1

        if request.user.is_authenticated:
            # D1 · the viewer's own offers here, so a helper can see (and change) their consent.
            mine = [{"offer_id": str(o.pk), "offer_type": o.offer_type, "status": o.status,
                     "contact_shared": o.contact_share_consent}
                    for o in r.offers.filter(account=request.user).order_by("created_at")]
            if mine:
                body["my_offers"] = mine
        # D1 + D8 · the other people on this rescue, and how to reach those who agreed. Absent
        # for anyone not on it — see sagip/contact.py for the table.
        people = contact.people_for(r, request.user)
        if people is not None:
            body["people"] = people

        return Response(body)


# S11 · a reporter's close is recorded on the status history (the single writer's note), so
# no column is needed and the reason survives in the audit trail where it belongs.
CLOSE_NOTE_PREFIX = "closed_by_reporter:"


def _close_reason(report, history):
    if report.status != StrayStatus.RESOLVED or not history:
        return None
    last = history[-1]
    if (last.changed_by_account_id == report.reporter_account_id
            and last.note.startswith(CLOSE_NOTE_PREFIX)):
        return last.note[len(CLOSE_NOTE_PREFIX):]
    return None


def _consent_share(request):
    s = ContactConsentSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    return s.validated_data["share"]


class ReportContactConsentView(APIView):
    """D1 · POST /reports/{id}/contact {share} — the reporter lets whoever claims this report
    see their phone and email, or withdraws that. D8 · not for an anonymous report."""
    permission_classes = [IsAuthenticated]

    def post(self, request, report_id):
        share = _consent_share(request)
        report = StrayReport.objects.filter(pk=report_id).first()
        if report is None:
            return Response({"error": {"code": "not_found", "message": "No such report"}}, status=404)
        if report.reporter_account_id != request.user.pk:
            return Response({"error": {"code": "not_your_report",
                                       "message": "Only the reporter can change this"}}, status=403)
        if share and report.is_anonymous:
            return Response({"error": {"code": "anonymous_report",
                                       "message": "An anonymous report can't share contact details"}},
                            status=409)
        return Response(contact.set_consent(report, share))


# D3 · a release is recorded on the status history's reopen row, like a reporter's close
# (S11), so a lapse and a release stay distinguishable without a new column.
RELEASE_NOTE_PREFIX = "released_by_claimer:"


class CaseReleaseView(APIView):
    """D3 · POST /cases/{id}/release {reason} — the claimer can't make it. The report reopens
    AT ONCE (instead of waiting out a 6–24 h claim window), the reporter and the helpers are
    told, and the case is kept with `expired_at` set, so it counts like a lapse — no penalty.

    Only while `claimed`. Once the animal is rescued the claimer has custody, and releasing
    would put it back on the street; the way out of custody is a handoff (list or place). The
    case and report rows are locked so a release can't race a status update or a second tap."""
    permission_classes = [IsAuthenticated]

    def post(self, request, case_id):
        s = ClaimReleaseSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        reason = s.validated_data["reason"]
        with transaction.atomic():
            case = (RescueCase.objects.select_for_update().select_related("report")
                    .filter(pk=case_id).first())
            if case is None:
                return Response({"error": {"code": "not_found", "message": "No such case"}},
                                status=404)
            if case.claimed_by_account_id != request.user.pk:
                return Response({"error": {"code": "not_your_case",
                                           "message": "Only the claimer can release this case"}},
                                status=403)
            if case.expired_at is not None:
                return Response({"error": {"code": "case_expired",
                                           "message": "This claim has already lapsed"}}, status=409)
            report = StrayReport.objects.select_for_update().get(pk=case.report_id)
            if report.status == StrayStatus.RESOLVED:
                return Response({"error": {"code": "case_resolved",
                                           "message": "This case is already resolved"}}, status=409)
            if report.status != StrayStatus.CLAIMED:
                return Response({"error": {"code": "in_custody",
                                           "message": "The animal is in your care — hand it off "
                                                      "instead of releasing the claim"}},
                                status=409)
            case.report = report
            reopen_case(case, request.user, f"{RELEASE_NOTE_PREFIX}{reason}")
            notices.claim_released(case, reason)
        emit("claim_released", reason=reason)
        return Response({"status": report.status})


class CaseContactConsentView(APIView):
    """D1 · POST /cases/{id}/contact {share} — the claimer lets the reporter and the matched
    helpers see their phone and email, or withdraws that. Only while the claim is live."""
    permission_classes = [IsAuthenticated]

    def post(self, request, case_id):
        share = _consent_share(request)
        case = RescueCase.objects.filter(pk=case_id).first()
        if case is None:
            return Response({"error": {"code": "not_found", "message": "No such case"}}, status=404)
        if case.claimed_by_account_id != request.user.pk:
            return Response({"error": {"code": "not_your_case",
                                       "message": "Only the claimer can change this"}}, status=403)
        if case.expired_at is not None:
            return Response({"error": {"code": "case_expired",
                                       "message": "This claim has lapsed"}}, status=409)
        return Response(contact.set_consent(case, share))


class OfferContactConsentView(APIView):
    """D1 · POST /reports/{id}/offers/{offer_id}/contact {share} — a helper lets whoever claims
    the report see their phone and email, or withdraws that."""
    permission_classes = [IsAuthenticated]

    def post(self, request, report_id, offer_id):
        share = _consent_share(request)
        offer = ReportOffer.objects.filter(pk=offer_id, report_id=report_id).first()
        if offer is None:
            return Response({"error": {"code": "not_found", "message": "No such offer"}}, status=404)
        if offer.account_id != request.user.pk:
            return Response({"error": {"code": "not_your_offer",
                                       "message": "Only the helper can change this"}}, status=403)
        return Response(contact.set_consent(offer, share))


class ReportCloseView(APIView):
    """S11 · POST /reports/{id}/close {reason} — the reporter says the report no longer needs
    anyone: the animal is gone, it's a duplicate, they handled it themselves, or it was a
    mistake. Only while `reported`: once claimed, a rescuer is on the way and closing would
    strand them (the claim either progresses or lapses back to `reported`).

    Closing resolves the report through the single status writer (so escalation stops on the
    next sweep and the map greys it) and expires its open offers, which have nothing left to
    attach to. The report row is locked, so a close and a claim can't both win."""
    permission_classes = [IsAuthenticated]

    def post(self, request, report_id):
        s = ReportCloseSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        reason = s.validated_data["reason"]
        with transaction.atomic():
            report = StrayReport.objects.select_for_update().filter(pk=report_id).first()
            if report is None:
                return Response({"error": {"code": "not_found", "message": "No such report"}},
                                status=404)
            if report.reporter_account_id != request.user.pk:
                return Response({"error": {"code": "not_your_report",
                                           "message": "Only the reporter can close this report"}},
                                status=403)
            if report.status != StrayStatus.REPORTED:
                return _report_not_open()
            set_report_status(report, StrayStatus.RESOLVED, request.user,
                              note=f"{CLOSE_NOTE_PREFIX}{reason}")
            ReportOffer.objects.filter(report=report, status=OfferStatus.OPEN).update(
                status=OfferStatus.EXPIRED)
        emit("report_closed", reason=reason)
        return Response({"report_id": str(report.pk), "status": report.status})


def _escalation_notified(report):
    """S5 · how many people each escalation level actually reached — counted from the
    notification rows the sweep wrote, never inferred from the level. The waiting view used to
    say "partner shelters notified" at level 2 when no partner could exist; with these counts
    the client can only claim what happened."""
    rows = (Notification.objects
            .filter(type="report_escalated", data__report_id=str(report.pk))
            .values("data__escalation_level").annotate(n=Count("pk")))
    reached = {row["data__escalation_level"]: row["n"] for row in rows}
    # D2 · the report-time alert. None when the policy sends nothing (a healthy stray, a lost
    # pet), so the client never reads "no one there to alert" when the truth is "we don't
    # alert for this".
    at_report = (Notification.objects.filter(type=alerts.ALERT_TYPE,
                                             data__report_id=str(report.pk)).count()
                 if alerts.alerts_at_report_apply(report) else None)
    return {"level_1": reached.get(1, 0), "level_2": reached.get(2, 0), "at_report": at_report}


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
