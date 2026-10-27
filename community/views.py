"""US-W1 · Abot-tulong wishlist endpoints (needs & pledges).

Every change to a need or a pledge — status, `quantity_received`, `decided_at`,
`cancel_reason`, `fulfilled_at`, the editable fields — goes through `community/needs.py`
(create_pledge / cancel_pledge / apply_received / edit_need / complete_need) under the need's
row lock (D-S6-7). The views validate, call one writer, and notify AFTER it has committed.
"""
from django.db.models import Count
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Account, AccountStatus
from common.analytics import emit
from common.throttles import NeedCreateThrottle, PledgeCreateThrottle, StoryCreateThrottle
from notifications.service import notify
from shelter.permissions import IsShelter, IsVerifiedShelterForNeeds

from .badges import impact_counts
from .models import (
    AccountBadge,
    NeedPledge,
    NeedStatus,
    ShelterNeed,
    StoryPhoto,
    StoryPost,
    StoryReaction,
    StoryStatus,
    StoryType,
)
from .needs import (
    NeedError,
    apply_received,
    cancel_pledge,
    complete_need,
    create_pledge,
    edit_need,
    owned_need_id,
)
from .reads import (
    donation_instructions,
    my_pledge_rows,
    need_rows,
    page_window,
    pledge_rows,
    trim_page,
    visible_needs,
    ways_to_help_rows,
)
from .serializers import (
    NeedCreateSerializer,
    NeedPatchSerializer,
    PledgeCreateSerializer,
    ReceivedSerializer,
    StoryCreateSerializer,
)

PAGE_SIZE = 20     # stories only; the wishlist lists page through `community/reads.py`


def _err(code, message, status):
    return Response({"error": {"code": code, "message": message}}, status=status)


_NEED_ERRORS = {
    # code: (status, default message)
    "not_found": (404, "No such need."),
    "own_need": (403, "A shelter can't pledge to its own wishlist."),
    "need_not_open": (409, "This need is no longer open for pledges."),
    "pledge_decided": (409, ""),
    "total_overflow": (422, "That would take this need's received total past the maximum."),
    "forbidden": (403, "Only the owning shelter can do that."),
    "below_received": (422, "The target can't go below what has already been received."),
}
_PLEDGE_DECIDED_MESSAGE = "This pledge has already been decided."


def _need_error(e, *, decided_message=_PLEDGE_DECIDED_MESSAGE):
    """Map a `NeedError` from `community/needs.py` onto the error envelope."""
    status, default = _NEED_ERRORS[e.code]
    if e.code == "pledge_decided":
        default = decided_message
    body = {"code": e.code, "message": e.message or default}
    if e.details is not None:
        body["details"] = e.details
    return Response({"error": body}, status=status)


def _need_repr(need, viewer):
    """One need as a row (the PATCH response). Same shape as a list row."""
    return need_rows(ShelterNeed.objects.filter(pk=need.pk), viewer)[0]


class ShelterNeedsView(APIView):
    """GET a shelter's wishlist (public, live shelters only — the owner always sees its own);
    POST a new need (that shelter only, and only once its organisation is approved)."""

    def get_permissions(self):
        return [IsVerifiedShelterForNeeds()] if self.request.method == "POST" else [AllowAny()]

    def get_throttles(self):
        # US-K2 · the WRITE is rate-limited; the public read is not. A class-level
        # throttle_classes would have quietly rationed the feed everyone browses.
        return [NeedCreateThrottle()] if self.request.method == "POST" else []

    def get(self, request, account_id):
        # AW2/AW31 · everyone but the owning shelter sees only a live shelter's needs (R3).
        qs = visible_needs(request.user).filter(shelter_account_id=account_id)
        status = request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        page, window = page_window(request)
        rows = need_rows(qs.order_by("-created_at", "need_id"), request.user, window)
        rows, nxt = trim_page(rows, page)
        return Response({"results": rows, "next": nxt,
                         "donation_instructions": donation_instructions(account_id, request.user)})

    def post(self, request, account_id):
        if str(request.user.pk) != str(account_id):
            return _err("forbidden", "A shelter can only post its own needs.", 403)
        s = NeedCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        need = ShelterNeed.objects.create(shelter_account=request.user, **s.validated_data)
        return Response({"need_id": str(need.pk)}, status=201)


def _notify_need_closed(need, shelter, cancelled):
    """One `need_closed` per pledge Mark complete just cancelled; nothing when none were."""
    for p in cancelled:
        notify(p.pledger_account, "need_closed", title="Need closed",
               body=(f"{shelter.display_name} closed “{need.title}” — your pledge was "
                     "cancelled. Salamat for offering."),
               data={"need_id": str(need.pk), "pledge_id": str(p.pk)})


class WaysToHelpView(APIView):
    """US-W4 · GET the verified shelters someone can help, near `?city=` first (public; 20 a page). Tapping one in the
    app opens its Donate screen."""
    permission_classes = [AllowAny]

    def get_throttles(self):
        return []   # a public read, like a shelter's wishlist (US-K2 rations writes only)

    def get(self, request):
        page, window = page_window(request)
        rows, nxt = trim_page(ways_to_help_rows(request.query_params.get("city"), window), page)
        return Response({"results": rows, "next": nxt})


class NeedDetailView(APIView):
    """US-W3 · GET one need (public); the owning shelter edits a need (while open) or closes
    it. Both go through the need-locked writers in `community/needs.py`; `status: "closed"` is
    what shipped app builds still send, and now means Mark complete (the cascade included)."""

    def get_permissions(self):
        return [IsShelter()] if self.request.method == "PATCH" else [AllowAny()]

    def get(self, request, need_id):
        """One need, with its aggregates, the shelter's name and drop-off instructions. Outside
        `live_needs()` it is a 404 for everyone but the owning shelter (R3)."""
        qs = visible_needs(request.user).filter(pk=need_id)
        account_id = qs.values_list("shelter_account_id", flat=True).first()
        if account_id is None:
            return _err("not_found", "No such need.", 404)
        row = need_rows(qs, request.user)[0]
        row.update({"shelter_name": Account.objects.get(pk=account_id).display_name,
                    "shelter_account_id": str(account_id),
                    "donation_instructions": donation_instructions(account_id, request.user)})
        return Response(row)

    def patch(self, request, need_id):
        try:
            owned_need_id(need_id, request.user, "Only the owning shelter can edit this need.")
        except NeedError as e:
            return _need_error(e)
        s = NeedPatchSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        try:
            if d.get("status") == NeedStatus.CLOSED:
                need, cancelled = complete_need(need_id)
                _notify_need_closed(need, request.user, cancelled)
            else:
                need = edit_need(need_id, request.user, d)
        except NeedError as e:
            return _need_error(e)
        return Response(_need_repr(need, request.user))


class NeedCompleteView(APIView):
    """POST Mark complete: `fulfilled` if the target was met, else `closed`, cancelling the
    still-open pledges (each donor is told). On a fulfilled need it only settles the pledges still
    waiting, and the need stays fulfilled. Idempotent: a repeat call, or a closed need, changes
    nothing."""
    permission_classes = [IsShelter]

    def post(self, request, need_id):
        try:
            owned_need_id(need_id, request.user, "Only the owning shelter can close this need.")
            need, cancelled = complete_need(need_id)
        except NeedError as e:
            return _need_error(e)
        _notify_need_closed(need, request.user, cancelled)
        return Response({"need_status": need.status, "cancelled_pledges": len(cancelled)})


class NeedPledgesView(APIView):
    """POST a pledge against an open need (any user); GET the need's pledges (owning shelter)."""

    def get_permissions(self):
        return [IsShelter()] if self.request.method == "GET" else [IsAuthenticated()]

    def get_throttles(self):
        return [PledgeCreateThrottle()] if self.request.method == "POST" else []

    def get(self, request, need_id):
        need = ShelterNeed.objects.filter(pk=need_id).first()
        if need is None:
            return _err("not_found", "No such need.", 404)
        if need.shelter_account_id != request.user.pk:
            return _err("forbidden", "Only the owning shelter can see these pledges.", 403)
        pledges = need.pledges.select_related("pledger_account").order_by("-created_at")
        return Response({"results": pledge_rows(pledges)})

    def post(self, request, need_id):
        s = PledgeCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            pledge = create_pledge(need_id, request.user, s.validated_data["quantity"],
                                   s.validated_data["note"])
        except NeedError as e:
            return _need_error(e)
        need = pledge.need
        notify(need.shelter_account, "pledge_received",
               title="New pledge", body=f"Someone pledged to “{need.title}”.",
               data={"need_id": str(need.pk), "pledge_id": str(pledge.pk)})
        emit("need_pledged", category=need.category)
        return Response({"pledge_id": str(pledge.pk)}, status=201)


class PledgeCancelView(APIView):
    """POST to cancel one's own still-`pledged` pledge. A delivered pledge is a recorded fact."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pledge_id):
        try:
            pledge = cancel_pledge(pledge_id, request.user)
        except NeedError as e:
            return _need_error(e, decided_message="This pledge can no longer be cancelled.")
        need = pledge.need
        notify(need.shelter_account, "pledge_cancelled", title="Pledge cancelled",
               body=f"{request.user.display_name} cancelled their pledge to “{need.title}”.",
               data={"need_id": str(need.pk), "pledge_id": str(pledge.pk)})
        return Response({"status": pledge.status})


class MyPledgesView(APIView):
    """US-W2 · the giver's own pledges (My Donations): pledged / delivered / cancelled."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        page, window = page_window(request)
        qs = (NeedPledge.objects.filter(pledger_account=request.user)
              .select_related("need", "need__shelter_account")
              .order_by("-created_at", "pledge_id")[window])
        rows, nxt = trim_page(list(qs), page)
        return Response({"results": my_pledge_rows(rows), "next": nxt})


class NeedReceivedView(APIView):
    """POST: the shelter confirms it received a pledge, entering the actual quantity."""
    permission_classes = [IsShelter]

    def post(self, request, need_id):
        need = ShelterNeed.objects.filter(pk=need_id).first()
        if need is None:
            return _err("not_found", "No such need.", 404)
        if need.shelter_account_id != request.user.pk:
            return _err("forbidden", "Only the owning shelter can confirm receipt.", 403)
        s = ReceivedSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        pledge_id = s.validated_data["pledge_id"]
        try:
            need = apply_received(need.pk, pledge_id, s.validated_data["quantity_received"])
        except NeedError as e:
            return _need_error(e)
        # `pledger_account` never changes, so reading it after the writer committed is safe.
        pledge = NeedPledge.objects.select_related("pledger_account").get(pk=pledge_id)
        notify(pledge.pledger_account, "pledge_confirmed",
               title="Pledge received",
               body=f"{request.user.display_name} received your pledge to “{need.title}”. Salamat!",
               data={"need_id": str(need.pk), "pledge_id": str(pledge.pk)})
        emit("pledge_delivered", category=need.category)
        return Response({"need_status": need.status})


class MeImpactView(APIView):
    """US-B1 · GET the caller's earned badges + impact aggregates. A pure read: awarding
    happens at the qualifying event and in the nightly sweep (D-S6-3), never here."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Return the WHOLE catalog with an earned flag, so the grid can show earned badges
        # bright and unearned ones dimmed-with-criteria (US-B2) from a single call — an
        # unearned badge is never rendered as earned (the Reliable-chip trust rule).
        from .models import Badge
        earned = {b.badge_id: b.earned_at
                  for b in AccountBadge.objects.filter(account=request.user)}
        catalog = Badge.objects.all().order_by("badge_code")
        badges = [{"badge_code": b.pk, "name": b.name, "description": b.description,
                   "icon": b.icon, "criteria": b.criteria,
                   "earned": b.pk in earned,
                   "earned_at": earned[b.pk].isoformat() if b.pk in earned else None}
                  for b in catalog]
        # earned first, then the rest — stable within each group.
        badges.sort(key=lambda x: (not x["earned"], x["badge_code"]))
        return Response({"impact": impact_counts(request.user), "badges": badges})


# --- US-T1 · success stories ---------------------------------------------------


def _author_city(account):
    # D-S6-4 · a story renders the author's city (city-level, like everywhere). An account's
    # city is its primary Address's city; accounts without one render an empty string.
    addr = next((a for a in account.addresses.all() if a.is_primary), None)
    return addr.city if addr else ""


def _story_repr(story, my_reacted_ids):
    return {"story_id": str(story.pk),
            "author": {"name": story.author_account.display_name,
                       "city": _author_city(story.author_account)},
            "story_type": story.story_type, "caption": story.caption, "status": story.status,
            "photos": [{"url": p.url, "is_primary": p.is_primary} for p in story.photos.all()],
            "reaction_count": getattr(story, "_rcount", None)
            if getattr(story, "_rcount", None) is not None else story.reactions.count(),
            "my_reaction": story.pk in my_reacted_ids}


class StoriesView(APIView):
    """US-T1 · GET the public story feed (published only, newest-first, city-filterable);
    POST a new story (min 1 photo — a story is a picture with a caption)."""

    def get_permissions(self):
        return [IsAuthenticated()] if self.request.method == "POST" else [AllowAny()]

    def get_throttles(self):
        return [StoryCreateThrottle()] if self.request.method == "POST" else []

    def get(self, request):
        # US-N1 · the feed drops a deleted author's stories (the rows survive for the
        # welfare record; the public surface does not show them).
        qs = (StoryPost.objects.filter(status=StoryStatus.PUBLISHED)
              .exclude(author_account__status=AccountStatus.DELETED)
              .select_related("author_account")
              .prefetch_related("photos", "author_account__addresses")
              .annotate(_rcount=Count("reactions")).order_by("-created_at"))
        city = request.query_params.get("city")
        if city:
            qs = qs.filter(author_account__addresses__is_primary=True,
                           author_account__addresses__city=city).distinct()
        page = list(qs[:PAGE_SIZE])
        mine = set()
        if request.user and request.user.is_authenticated:
            mine = set(StoryReaction.objects
                       .filter(account=request.user, story__in=[s.pk for s in page])
                       .values_list("story_id", flat=True))
        return Response({"results": [_story_repr(s, mine) for s in page]})

    def post(self, request):
        s = StoryCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        if not d["photos"]:
            return _err("photo_required", "A story needs at least one photo.", 422)
        # Type auto-derives from the link (T0): an adoption story if it cites a listing, a
        # rescue story if it cites a case, else whatever was chosen (default general).
        if d.get("adoption_listing_id"):
            story_type = StoryType.ADOPTION
        elif d.get("rescue_case_id"):
            story_type = StoryType.RESCUE
        else:
            story_type = d.get("story_type") or StoryType.GENERAL
        story = StoryPost.objects.create(
            author_account=request.user, caption=d["caption"], story_type=story_type,
            adoption_listing_id=d.get("adoption_listing_id"),
            rescue_case_id=d.get("rescue_case_id"))
        for ph in d["photos"]:
            StoryPhoto.objects.create(story=story, url=ph["file_url"],
                                      is_primary=ph.get("is_primary", False))
        emit("story_posted", story_type=story_type)
        return Response({"story_id": str(story.pk)}, status=201)


class StoryDetailView(APIView):
    """US-T1 · one story. A hidden story 404s for everyone but its author, who sees it with its
    `hidden` status (moderation is never a silent vanish)."""
    permission_classes = [AllowAny]

    def get(self, request, story_id):
        story = (StoryPost.objects.select_related("author_account")
                 .prefetch_related("photos", "author_account__addresses")
                 .filter(pk=story_id).first())
        if story is None:
            return _err("not_found", "No such story.", 404)
        is_author = (request.user and request.user.is_authenticated
                     and request.user.pk == story.author_account_id)
        if story.status == StoryStatus.HIDDEN and not is_author:
            return _err("not_found", "No such story.", 404)
        mine = set()
        if request.user and request.user.is_authenticated:
            if StoryReaction.objects.filter(account=request.user, story=story).exists():
                mine = {story.pk}
        return Response(_story_repr(story, mine))


class StoryReactionView(APIView):
    """US-T1 · react / un-react. Idempotent via uq_story_reaction: POST twice is still one row."""
    permission_classes = [IsAuthenticated]

    def _count(self, story):
        return StoryReaction.objects.filter(story=story).count()

    def post(self, request, story_id):
        story = StoryPost.objects.filter(pk=story_id, status=StoryStatus.PUBLISHED).first()
        if story is None:
            return _err("not_found", "No such story.", 404)
        _, created = StoryReaction.objects.get_or_create(story=story, account=request.user)
        if created:
            emit("story_reacted", story_type=story.story_type)
        return Response({"reaction_count": self._count(story)})

    def delete(self, request, story_id):
        story = StoryPost.objects.filter(pk=story_id).first()
        if story is None:
            return _err("not_found", "No such story.", 404)
        StoryReaction.objects.filter(story=story, account=request.user).delete()
        return Response({"reaction_count": self._count(story)})
