import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from listings.models import AdoptionListing
from notifications.service import notify
from shelter.permissions import IsShelter
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.reliability import reliability_for, reliability_for_many
from volunteer.serializers import (AttendanceSerializer, ShiftCreateSerializer,
                                   ShiftPatchSerializer, SignupCreateSerializer)
from volunteer.status import set_signup_status

PAGE_SIZE = 20

# Annotates a shift queryset with its approved-signup tally so a list of shifts costs one
# query instead of a per-row COUNT (the N+1 `_shift_repr` would otherwise incur per page).
_APPROVED_COUNT = Count("signups", filter=Q(signups__status=SignupStatus.APPROVED))


def _not_found(what="shift"):
    return Response({"error": {"code": "not_found", "message": f"No such {what}"}}, status=404)


def _shift_repr(shift, approved_count=None):
    if approved_count is None:
        approved_count = shift.signups.filter(status=SignupStatus.APPROVED).count()
    return {"shift_id": str(shift.pk), "type": shift.type,
            "org_name": shift.shelter_account.display_name,
            "starts_at": shift.starts_at.isoformat(), "ends_at": shift.ends_at.isoformat(),
            "capacity": shift.capacity, "status": shift.status,
            "slots_left": max(shift.capacity - approved_count, 0)}


def _my_shift_repr(shift):
    return {"shift_id": str(shift.pk), "type": shift.type,
            "org_name": shift.shelter_account.display_name,
            "starts_at": shift.starts_at.isoformat(), "ends_at": shift.ends_at.isoformat(),
            "status": shift.status, "capacity": shift.capacity}


def _my_item_repr(su, now):
    late = (su.status == SignupStatus.CANCELLED and su.cancelled_at is not None
            and su.cancelled_at > su.shift.starts_at - timezone.timedelta(hours=CANCEL_CUTOFF_HOURS))
    hours = None
    if su.check_in_at and su.check_out_at:
        hours = round((su.check_out_at - su.check_in_at).total_seconds() / 3600, 1)
    return {"signup_id": str(su.pk), "status": su.status,
            "cancelled_at": su.cancelled_at.isoformat() if su.cancelled_at else None,
            "was_late": late,
            "check_in_at": su.check_in_at.isoformat() if su.check_in_at else None,
            "check_out_at": su.check_out_at.isoformat() if su.check_out_at else None,
            "hours": hours, "shift": _my_shift_repr(su.shift)}


class ShelterShiftsView(APIView):
    """US-V2 · a shelter posts and lists its own activities."""
    permission_classes = [IsShelter]

    def post(self, request):
        s = ShiftCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        if d["ends_at"] <= d["starts_at"]:
            return Response({"error": {"code": "bad_window",
                                       "message": "The activity must end after it starts"}},
                            status=422)
        shift = VolunteerShift.objects.create(shelter_account=request.user, **d)
        return Response(_shift_repr(shift, approved_count=0), status=201)

    def get(self, request):
        qs = (VolunteerShift.objects.filter(shelter_account=request.user)
              .select_related("shelter_account")
              .annotate(approved_count=_APPROVED_COUNT)
              .order_by("starts_at"))
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response({"results": [_shift_repr(s, approved_count=s.approved_count)
                                     for s in qs[:PAGE_SIZE]],
                         "next": None})


class ShelterShiftDetailView(APIView):
    """US-V2 · edit one activity. `closed` is terminal — never reopened."""
    permission_classes = [IsShelter]

    def get(self, request, shift_id):
        shift = (VolunteerShift.objects.select_related("shelter_account")
                 .filter(pk=shift_id).first())
        if shift is None:
            return _not_found()
        if shift.shelter_account_id != request.user.pk:
            return Response({"error": {"code": "not_your_shift",
                                       "message": "Only the posting shelter can view this"}},
                            status=403)
        return Response(_shift_repr(shift))

    def patch(self, request, shift_id):
        shift = (VolunteerShift.objects.filter(pk=shift_id)
                 .select_related("shelter_account").first())
        if shift is None:
            return _not_found()
        if shift.shelter_account_id != request.user.pk:
            return Response({"error": {"code": "not_your_shift",
                                       "message": "Only the posting shelter can edit this"}},
                            status=403)
        if shift.status == ShiftStatus.CLOSED:
            return Response({"error": {"code": "shift_closed",
                                       "message": "A closed activity cannot be changed"}},
                            status=409)
        s = ShiftPatchSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        for key, value in s.validated_data.items():
            setattr(shift, key, value)
        if shift.ends_at <= shift.starts_at:
            return Response({"error": {"code": "bad_window",
                                       "message": "The activity must end after it starts"}},
                            status=422)
        shift.save()
        return Response(_shift_repr(shift))


class ShelterShiftCancelView(APIView):
    """US-V2 · cancelling an activity cascades: the shift closes, every attached signup is
    cancelled, capacity releases, and every signed-up volunteer is notified. One transaction —
    a half-cancelled activity strands people who think they are still booked."""
    permission_classes = [IsShelter]

    def post(self, request, shift_id):
        shift = VolunteerShift.objects.filter(pk=shift_id).first()
        if shift is None:
            return _not_found()
        if shift.shelter_account_id != request.user.pk:
            return Response({"error": {"code": "not_your_shift",
                                       "message": "Only the posting shelter can cancel this"}},
                            status=403)
        if shift.status == ShiftStatus.CLOSED:
            return Response({"error": {"code": "shift_closed",
                                       "message": "This activity is already closed"}}, status=409)

        live = [SignupStatus.REQUESTED, SignupStatus.APPROVED]
        with transaction.atomic():
            shift.status = ShiftStatus.CLOSED
            shift.save(update_fields=["status", "updated_at"])
            affected = list(shift.signups.select_for_update().filter(status__in=live))
            for signup in affected:
                set_signup_status(signup, SignupStatus.CANCELLED)
                notify(signup.volunteer_account, "shift_cancelled_by_shelter",
                       title="An activity you signed up for was cancelled",
                       body="The shelter cancelled this activity.",
                       data={"shift_id": str(shift.pk)})
        return Response({"cancelled_signups": len(affected)})


class ShiftsBrowseView(APIView):
    """US-V3 · public browse. Guests may look; requesting requires an account.

    Shows `open` and `full` shifts with a future start. `full` is transient, not
    terminal — a cancelled signup flips the shift back to `open` — so a full shift is
    still a live candidate, not dead listing inventory; it's returned with
    `slots_left: 0` and the client decides how to render it (e.g. disabled). Only
    `closed` (terminal) and past shifts are excluded.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        qs = (VolunteerShift.objects
              .filter(status__in=[ShiftStatus.OPEN, ShiftStatus.FULL],
                      starts_at__gt=timezone.now())
              .select_related("shelter_account")
              .annotate(approved_count=_APPROVED_COUNT)
              .order_by("starts_at"))
        shift_type = request.query_params.get("type")
        if shift_type:
            qs = qs.filter(type=shift_type)
        return Response({"results": [_shift_repr(s, approved_count=s.approved_count)
                                     for s in qs[:PAGE_SIZE]], "next": None})


class ShiftDetailView(APIView):
    """US-V3 · public shift detail, carrying `slots_left` for the '4 of 6 left' line."""
    permission_classes = [AllowAny]

    def get(self, request, shift_id):
        shift = (VolunteerShift.objects.filter(pk=shift_id)
                 .select_related("shelter_account").first())
        if shift is None:
            return _not_found()
        return Response(_shift_repr(shift))


class MySignupsView(APIView):
    """US-V8 · the volunteer's own signups — the read surface the schedule/history screens need.

    Read-side derivation, no stored buckets. `was_late` and the reliability block are
    server-derived (D-S5-2 aggregate-only; never a client clock). Only the caller's own
    signups are ever returned.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        now = timezone.now()
        qs = (VolunteerSignup.objects.filter(volunteer_account=request.user)
              .select_related("shift", "shift__shelter_account").order_by("-shift__starts_at"))
        requested, upcoming, history = [], [], []
        for su in qs:
            item = _my_item_repr(su, now)
            if su.status == SignupStatus.REQUESTED:
                requested.append(item)
            elif su.status == SignupStatus.APPROVED and su.shift.ends_at > now:
                upcoming.append(item)
            else:
                history.append(item)
        return Response({"requested": requested, "upcoming": upcoming, "history": history,
                         "reliability": reliability_for(request.user)})


class ShiftSignupView(APIView):
    """US-V3 · request a shift.

    Two separate consents (see VolunteerSignup's docstring). The waiver is required and
    versioned (D-S5-1); contact-sharing is optional, per-shift, and only stamped when given.
    `UNIQUE (shift, volunteer)` is enforced by the DB and caught here so a double-tap is a
    clean 409 rather than a 500.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, shift_id):
        shift = VolunteerShift.objects.filter(pk=shift_id).first()
        if shift is None:
            return _not_found()
        if shift.status != ShiftStatus.OPEN:
            return Response({"error": {"code": "shift_not_open",
                                       "message": "This activity is not taking requests"}},
                            status=409)

        s = SignupCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        if not d["waiver_accepted"]:
            return Response({"error": {"code": "waiver_required",
                                       "message": "The waiver must be accepted to join"}},
                            status=422)

        now = timezone.now()
        sharing = d["contact_share_consent"]
        try:
            signup = VolunteerSignup.objects.create(
                shift=shift, volunteer_account=request.user,
                waiver_accepted=True, waiver_accepted_at=now,
                waiver_version=settings.WAIVER_VERSION,
                contact_share_consent=sharing,
                contact_share_consent_at=now if sharing else None)
        except IntegrityError:
            return Response({"error": {"code": "already_requested",
                                       "message": "You already requested this activity"}},
                            status=409)

        notify(shift.shelter_account, "signup_requested",
               title="A volunteer requested a shift",
               body=f"{request.user.display_name} wants to join.",
               data={"shift_id": str(shift.pk), "signup_id": str(signup.pk)})
        return Response({"signup_id": str(signup.pk), "status": signup.status}, status=201)


def _contact_repr(account):
    addr = account.addresses.filter(is_primary=True).first() or account.addresses.first()
    return {"phone": account.phone, "email": account.email,
            "address": ({"line1": addr.line1, "barangay": addr.barangay, "city": addr.city,
                         "province": addr.province} if addr else None)}


def _load_signup_for_shelter(signup_id, user):
    """Fetch a signup and confirm the caller's shelter posted its shift.
    Returns (signup, None) or (None, error_response)."""
    signup = (VolunteerSignup.objects.select_related("shift", "volunteer_account")
              .filter(pk=signup_id).first())
    if signup is None:
        return None, _not_found("signup")
    if signup.shift.shelter_account_id != user.pk:
        return None, Response({"error": {"code": "not_your_shift",
                                         "message": "Only the posting shelter can do this"}},
                              status=403)
    return signup, None


class SignupApproveView(APIView):
    """US-V4 · approve a request, capacity-safe.

    ⚠️ The capacity check recounts INSIDE `transaction.atomic()` after
    `select_for_update()` on the shift row. A plain count-then-write lets two concurrent
    approvals both observe a free slot and both commit, overfilling the shift — the
    difference between `capacity` being an enforced invariant and a UI-only suggestion.
    Same pattern as `sagip/views.py::ReportClaimView`'s claim exclusivity.

    ⚠️ The `not_pending` status check is ALSO re-verified under lock, on a freshly
    re-fetched signup row, after the shift lock is taken. Reading `signup.status` only
    before the lock (as the first cut of this view did) is a TOCTOU hole: two concurrent
    requests on the same signup — a double-tap, a client retry, or a decline racing an
    approve — can both pass the pre-lock check before either commits, letting a stale
    in-memory `signup` silently overwrite a DECLINED row back to APPROVED, or double-apply
    an approval. Lock order is always shift-then-signup (see SignupDeclineView, which
    takes only the signup lock and never the shift's) so the two views can never deadlock
    against each other.
    """
    permission_classes = [IsShelter]

    def post(self, request, signup_id):
        signup, error = _load_signup_for_shelter(signup_id, request.user)
        if error:
            return error

        listing_id = request.data.get("assigned_listing_id")
        listing = None
        if listing_id:
            try:
                uuid.UUID(str(listing_id))
            except (ValueError, TypeError):
                return Response({"error": {"code": "bad_listing",
                                           "message": "Pick one of your own animals for a walking shift"}},
                                status=422)
            listing = AdoptionListing.objects.filter(pk=listing_id,
                                                     posted_by=request.user).first()
            if listing is None or signup.shift.type != "walking":
                return Response({"error": {"code": "bad_listing",
                                           "message": "Pick one of your own animals for a walking shift"}},
                                status=422)

        rel = reliability_for(signup.volunteer_account)
        if rel["needs_reapproval"] and request.data.get("acknowledged_reapproval") is not True:
            # Server-enforced, not UI-enforced: a client must not skip the disclosure by
            # omitting it — or by sending a non-`true` value. Only a real JSON `true` counts
            # as acknowledged (a truthy string like "false" must NOT). The shelter may still
            # approve — this is a gate, never a ban.
            return Response({"error": {"code": "reapproval_required",
                                       "message": "This volunteer has 3 no-shows in a row",
                                       "details": rel}}, status=409)

        with transaction.atomic():
            shift = (VolunteerShift.objects.select_for_update()
                     .get(pk=signup.shift_id))          # lock 1: the shift
            signup = (VolunteerSignup.objects.select_for_update()
                      .get(pk=signup.pk))                # lock 2: the signup
            if signup.status != SignupStatus.REQUESTED:
                return Response({"error": {"code": "not_pending",
                                           "message": "This request was already decided"}},
                                status=409)

            approved = shift.signups.filter(status=SignupStatus.APPROVED).count()
            if approved >= shift.capacity:
                return Response({"error": {"code": "shift_full",
                                           "message": "This activity is already full"}},
                                status=409)

            set_signup_status(signup, SignupStatus.APPROVED)
            if listing is not None:
                signup.assigned_listing = listing
                signup.save(update_fields=["assigned_listing", "updated_at"])
            if approved + 1 >= shift.capacity and shift.status == ShiftStatus.OPEN:
                shift.status = ShiftStatus.FULL
                shift.save(update_fields=["status", "updated_at"])

            notify(signup.volunteer_account, "shift_confirmed",
                   title="Your shift is confirmed",
                   body="The shelter approved your request.",
                   data={"shift_id": str(shift.pk), "signup_id": str(signup.pk)})
        return Response({"status": SignupStatus.APPROVED})


class SignupDeclineView(APIView):
    """US-V4 · decline a request. Always allowed, never automatic, and never silent — a
    declined volunteer must not be left in an indefinite pending state.

    ⚠️ Locks ONLY the signup row (never the shift) — declining frees no capacity, so it
    has nothing to recount. This IS still a real lock, unlike the first cut of this view:
    an approve racing the same signup takes shift-then-signup, so a lock here is required
    to close the same TOCTOU hole described on SignupApproveView (an unlocked decline
    could silently lose to, or be silently overwritten by, a concurrent approve on the
    same row). Because this view never acquires the shift lock, it can never deadlock
    against SignupApproveView's shift→signup order — it only ever waits on the signup,
    which approve also always acquires last.
    """
    permission_classes = [IsShelter]

    def post(self, request, signup_id):
        signup, error = _load_signup_for_shelter(signup_id, request.user)
        if error:
            return error

        with transaction.atomic():
            signup = (VolunteerSignup.objects.select_for_update()
                      .get(pk=signup.pk))                # the only lock this view takes
            if signup.status != SignupStatus.REQUESTED:
                return Response({"error": {"code": "not_pending",
                                           "message": "This request was already decided"}},
                                status=409)
            set_signup_status(signup, SignupStatus.DECLINED)
            notify(signup.volunteer_account, "signup_declined",
                   title="Your shift request wasn't accepted",
                   body="The shelter declined this request.",
                   data={"shift_id": str(signup.shift_id), "signup_id": str(signup.pk)})
        return Response({"status": SignupStatus.DECLINED})


# Decision 14 · a volunteer cancels FREE up to 12h before the shift; later cancels are still
# allowed but recorded on their record. Policy number, not a magic constant.
CANCEL_CUTOFF_HOURS = 12


class SignupCancelView(APIView):
    """US-V6 · a volunteer cancels their own signup.

    ⚠️ Lateness is computed SERVER-side from `cancelled_at` vs `starts_at`. A client cannot
    be trusted with the free-vs-recorded line, the same posture as the tier-derived document
    rules and US-V5's disclosure. `was_late` is derived on read, never stored as a flag.

    Cancelling releases capacity, so a `full` shift returns to `open` — under the same row
    lock the approve path uses, for consistency. `closed` stays terminal.

    ⚠️ The `not_cancellable` status check is re-verified under lock, on a freshly re-fetched
    signup row, after the shift lock is taken — same TOCTOU fix as SignupApproveView (see its
    docstring), propagated here. Two concurrent cancels (double-tap, mobile retry-on-timeout)
    both pass the pre-lock ownership check before either commits; without a signup-row lock and
    a fresh re-check, the second request's stale in-memory `signup` would let `set_signup_status`
    clobber `cancelled_at` with the second, later timestamp — destroying the exact instant the
    12h free-vs-late audit depends on. Lock order is shift-then-signup, same as
    SignupApproveView, so the two views can never deadlock against each other.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, signup_id):
        signup = (VolunteerSignup.objects.select_related("shift")
                  .filter(pk=signup_id).first())
        if signup is None:
            return _not_found("signup")
        if signup.volunteer_account_id != request.user.pk:
            return Response({"error": {"code": "not_your_signup",
                                       "message": "You can only cancel your own signup"}},
                            status=403)

        now = timezone.now()
        with transaction.atomic():
            shift = (VolunteerShift.objects.select_for_update()
                     .get(pk=signup.shift_id))              # lock 1: the shift
            signup = (VolunteerSignup.objects.select_for_update()
                      .get(pk=signup.pk))                    # lock 2: the signup
            if signup.status not in (SignupStatus.REQUESTED, SignupStatus.APPROVED):
                return Response({"error": {"code": "not_cancellable",
                                           "message": "This signup can no longer be cancelled"}},
                                status=409)

            cutoff = shift.starts_at - timezone.timedelta(hours=CANCEL_CUTOFF_HOURS)
            was_late = now > cutoff

            set_signup_status(signup, SignupStatus.CANCELLED, now=now)
            if shift.status == ShiftStatus.FULL:
                approved = shift.signups.filter(status=SignupStatus.APPROVED).count()
                if approved < shift.capacity:
                    shift.status = ShiftStatus.OPEN
                    shift.save(update_fields=["status", "updated_at"])
        return Response({"status": SignupStatus.CANCELLED, "was_late": was_late})


_TERMINAL = {SignupStatus.CANCELLED, SignupStatus.DECLINED}


class ShelterSignupVolunteerView(APIView):
    """US-P0 · the shelter's gated view of one volunteer (for shelter-volunteer-detail).

    Field-level authorization — the contact block is the ONLY sensitive surface and it is
    withheld unless the volunteer opted in for THIS shift and the signup is still live:

    | field            | when included                                              |
    |------------------|-----------------------------------------------------------|
    | display_name     | always (the shelter already sees it on the requests list) |
    | reliability      | always (aggregate-only, D-S5-2)                            |
    | contact          | ONLY if contact_share_consent AND status not terminal     |

    `masked_contact` is the platform default; contact-sharing is the documented §12.5
    exception, so it needs a gate and this view owns it. The key is ABSENT (not null) when
    withheld — the client renders "not shared", it never receives a null to leak.
    """
    permission_classes = [IsShelter]

    def get(self, request, signup_id):
        signup, error = _load_signup_for_shelter(signup_id, request.user)
        if error:
            return error
        body = {"display_name": signup.volunteer_account.display_name,
                "reliability": reliability_for(signup.volunteer_account)}
        if signup.contact_share_consent and signup.status not in _TERMINAL:
            body["contact"] = _contact_repr(signup.volunteer_account)
        return Response(body)


class ShiftRequestsView(APIView):
    """US-V5 · pending requests for one activity, each carrying its derived reliability
    block. ⚠️ Only the four aggregate numbers cross this boundary (D-S5-2) — the payload is
    built field-by-field rather than serialized off the related objects, so a stray
    `select_related` can't leak another shelter's identity into it."""
    permission_classes = [IsShelter]

    def get(self, request, shift_id):
        shift = VolunteerShift.objects.filter(pk=shift_id).first()
        if shift is None:
            return _not_found()
        if shift.shelter_account_id != request.user.pk:
            return Response({"error": {"code": "not_your_shift",
                                       "message": "Only the posting shelter can see these"}},
                            status=403)
        pending = list(shift.signups.filter(status=SignupStatus.REQUESTED)
                       .select_related("volunteer_account").order_by("created_at"))
        # Batch the reliability aggregates for the pending volunteers in a bounded number of
        # queries instead of ~4 per row. Response shape is unchanged.
        reliability = reliability_for_many(su.volunteer_account for su in pending)
        return Response({"results": [{
            "signup_id": str(su.pk),
            "volunteer": {"display_name": su.volunteer_account.display_name},
            "requested_at": su.created_at.isoformat(),
            "reliability": reliability[su.volunteer_account_id],
        } for su in pending]})


_ROSTER_STATUSES = (SignupStatus.APPROVED, SignupStatus.COMPLETED, SignupStatus.NO_SHOW)


class ShelterShiftRosterView(APIView):
    """US-V9 · the shift's attendance-relevant roster — approved signups still to be marked,
    plus completed/no_show ones already marked, so the mobile attendance screen has one list
    to render Attended/No-show against. `requested`, `declined`, and `cancelled` are excluded:
    they are not attendance-relevant."""
    permission_classes = [IsShelter]

    def get(self, request, shift_id):
        shift = VolunteerShift.objects.filter(pk=shift_id).first()
        if shift is None:
            return _not_found()
        if shift.shelter_account_id != request.user.pk:
            return Response({"error": {"code": "not_your_shift",
                                       "message": "Only the posting shelter can see this"}},
                            status=403)
        signups = (shift.signups.filter(status__in=_ROSTER_STATUSES)
                   .select_related("volunteer_account").order_by("created_at"))
        return Response({"results": [{
            "signup_id": str(su.pk),
            "volunteer": {"display_name": su.volunteer_account.display_name},
            "status": su.status,
            "check_in_at": su.check_in_at.isoformat() if su.check_in_at else None,
            "check_out_at": su.check_out_at.isoformat() if su.check_out_at else None,
        } for su in signups]})


class SignupCheckView(APIView):
    """US-V7 · the volunteer checks in and out on the day. Only an approved signup can —
    a requested or cancelled one has nothing to check into."""
    permission_classes = [IsAuthenticated]

    def post(self, request, signup_id, action):
        signup = VolunteerSignup.objects.filter(pk=signup_id).first()
        if signup is None:
            return _not_found("signup")
        if signup.volunteer_account_id != request.user.pk:
            return Response({"error": {"code": "not_your_signup",
                                       "message": "You can only check into your own shift"}},
                            status=403)
        if signup.status != SignupStatus.APPROVED:
            return Response({"error": {"code": "not_approved",
                                       "message": "This shift isn't confirmed"}}, status=409)
        field = "check_in_at" if action == "in" else "check_out_at"
        setattr(signup, field, timezone.now())
        signup.save(update_fields=[field, "updated_at"])
        return Response({field: getattr(signup, field).isoformat()})


class SignupAttendanceView(APIView):
    """US-V7 · after the shift, the shelter records what happened. `no_show` feeds the
    derived re-approval gate (US-V5), so it cannot be marked before the shift has ended —
    otherwise a shelter could flag someone for missing a shift still in the future.

    ⚠️ The `not_approved` status check is re-verified under lock, on a freshly re-fetched
    signup row — same TOCTOU fix as SignupDeclineView (see its docstring), the one status
    transition the branch had left unlocked. A volunteer may still cancel an `approved`
    signup after the shift has started (cancel gates on status, not time), so at the moment
    the shelter marks attendance the volunteer cancelling the SAME row is a real race:
    without the lock both sides pass their `status == APPROVED` check and the last writer
    wins — a `cancelled` signup silently overwritten to `no_show` (unfairly flagged), or a
    `completed` clobbered back to `cancelled` (losing a completion the reliability gate
    counts). Locks ONLY the signup row (never the shift): attendance frees no capacity, so
    it has nothing to recount, and taking no shift lock keeps it deadlock-free against the
    shift→signup holders, exactly like SignupDeclineView.
    """
    permission_classes = [IsShelter]

    def post(self, request, signup_id):
        signup, error = _load_signup_for_shelter(signup_id, request.user)
        if error:
            return error
        if signup.status != SignupStatus.APPROVED:
            return Response({"error": {"code": "not_approved",
                                       "message": "Only a confirmed shift has attendance"}},
                            status=409)
        if timezone.now() < signup.shift.ends_at:
            return Response({"error": {"code": "shift_not_ended",
                                       "message": "Attendance is recorded after the shift"}},
                            status=409)
        s = AttendanceSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        outcome = (SignupStatus.COMPLETED if s.validated_data["outcome"] == "completed"
                   else SignupStatus.NO_SHOW)

        with transaction.atomic():
            signup = (VolunteerSignup.objects.select_for_update()
                      .get(pk=signup.pk))                # the only lock this view takes
            if signup.status != SignupStatus.APPROVED:
                return Response({"error": {"code": "not_approved",
                                           "message": "Only a confirmed shift has attendance"}},
                                status=409)
            set_signup_status(signup, outcome)
        if outcome == SignupStatus.COMPLETED:
            # US-B1 · a completed shift can earn a badge. Idempotent + reconciled nightly,
            # so this immediacy hook never double-awards (deferred import avoids a cycle).
            from community.badges import award_badges_for
            award_badges_for(signup.volunteer_account)
        from common.analytics import emit
        emit("shift_completed" if outcome == SignupStatus.COMPLETED else "shift_no_show")
        return Response({"status": outcome})
