"""US-W1 · Abot-tulong wishlist endpoints (needs & pledges).

The `quantity_received` math and the `open -> fulfilled` flip are never done here — they go
through `community/needs.py::apply_received` under a row lock (D-S6-7).
"""
from django.db.models import Count
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import AccountStatus
from common.analytics import emit
from common.throttles import NeedCreateThrottle, PledgeCreateThrottle, StoryCreateThrottle
from notifications.service import notify
from shelter.permissions import IsShelter

from .badges import impact_counts
from .models import (
    AccountBadge,
    NeedPledge,
    NeedStatus,
    PledgeStatus,
    ShelterNeed,
    StoryPhoto,
    StoryPost,
    StoryReaction,
    StoryStatus,
    StoryType,
)
from .needs import NeedError, apply_received
from .serializers import (
    NeedCreateSerializer,
    NeedPatchSerializer,
    PledgeCreateSerializer,
    ReceivedSerializer,
    StoryCreateSerializer,
)

PAGE_SIZE = 20


def _err(code, message, status):
    return Response({"error": {"code": code, "message": message}}, status=status)


def _need_repr(need):
    return {"need_id": str(need.pk), "title": need.title, "category": need.category,
            "description": need.description, "quantity_needed": need.quantity_needed,
            "quantity_received": need.quantity_received, "status": need.status}


class ShelterNeedsView(APIView):
    """GET a shelter's wishlist (public); POST a new need (that shelter only)."""

    def get_permissions(self):
        return [IsShelter()] if self.request.method == "POST" else [AllowAny()]

    def get_throttles(self):
        # US-K2 · the WRITE is rate-limited; the public read is not. A class-level
        # throttle_classes would have quietly rationed the feed everyone browses.
        return [NeedCreateThrottle()] if self.request.method == "POST" else []

    def get(self, request, account_id):
        qs = ShelterNeed.objects.filter(shelter_account_id=account_id)
        status = request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        qs = qs.order_by("-created_at")[:PAGE_SIZE]
        return Response({"results": [_need_repr(n) for n in qs]})

    def post(self, request, account_id):
        if str(request.user.pk) != str(account_id):
            return _err("forbidden", "A shelter can only post its own needs.", 403)
        s = NeedCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        need = ShelterNeed.objects.create(shelter_account=request.user, **s.validated_data)
        return Response({"need_id": str(need.pk)}, status=201)


class NeedDetailView(APIView):
    """US-W3 · the owning shelter edits a need (while open) or closes it."""
    permission_classes = [IsShelter]

    def patch(self, request, need_id):
        need = ShelterNeed.objects.filter(pk=need_id).first()
        if need is None:
            return _err("not_found", "No such need.", 404)
        if need.shelter_account_id != request.user.pk:
            return _err("forbidden", "Only the owning shelter can edit this need.", 403)
        s = NeedPatchSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        if d.get("status") == NeedStatus.CLOSED:
            need.status = NeedStatus.CLOSED
            need.save(update_fields=["status"])
            return Response(_need_repr(need))
        if need.status != NeedStatus.OPEN:
            return _err("need_not_open", "Only an open need can be edited.", 409)
        for f in ("title", "description", "quantity_needed"):
            if f in d:
                setattr(need, f, d[f])
        need.save()
        return Response(_need_repr(need))


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
        pledges = (need.pledges.select_related("pledger_account").order_by("-created_at"))
        return Response({"results": [
            {"pledge_id": str(p.pk), "quantity": p.quantity, "status": p.status,
             "pledger_name": p.pledger_account.display_name,
             "created_at": p.created_at.isoformat()} for p in pledges]})

    def post(self, request, need_id):
        need = ShelterNeed.objects.filter(pk=need_id).first()
        if need is None:
            return _err("not_found", "No such need.", 404)
        if need.status != NeedStatus.OPEN:
            return _err("need_not_open", "This need is no longer open for pledges.", 409)
        s = PledgeCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        pledge = NeedPledge.objects.create(need=need, pledger_account=request.user,
                                           quantity=s.validated_data["quantity"])
        notify(need.shelter_account, "pledge_received",
               title="New pledge", body=f"Someone pledged to “{need.title}”.",
               data={"need_id": str(need.pk), "pledge_id": str(pledge.pk)})
        emit("need_pledged", category=need.category)
        return Response({"pledge_id": str(pledge.pk)}, status=201)


class PledgeCancelView(APIView):
    """POST to cancel one's own still-`pledged` pledge. A delivered pledge is a recorded fact."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pledge_id):
        pledge = NeedPledge.objects.filter(pk=pledge_id,
                                           pledger_account=request.user).first()
        if pledge is None:
            return _err("not_found", "No such pledge.", 404)
        if pledge.status != PledgeStatus.PLEDGED:
            return _err("pledge_decided", "This pledge can no longer be cancelled.", 409)
        pledge.status = PledgeStatus.CANCELLED
        pledge.save(update_fields=["status"])
        return Response({"status": pledge.status})


class MyPledgesView(APIView):
    """US-W2 · the giver's own pledges (My Donations): pledged / delivered / cancelled."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (NeedPledge.objects.filter(pledger_account=request.user)
              .select_related("need", "need__shelter_account").order_by("-created_at")[:PAGE_SIZE])
        return Response({"results": [
            {"pledge_id": str(p.pk), "quantity": p.quantity, "status": p.status,
             "created_at": p.created_at.isoformat(),
             "need": {"need_id": str(p.need_id), "title": p.need.title,
                      "category": p.need.category,
                      "shelter_name": p.need.shelter_account.display_name}}
            for p in qs]})


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
        pledge = NeedPledge.objects.filter(pk=s.validated_data["pledge_id"],
                                           need=need).first()
        if pledge is None:
            return _err("not_found", "No such pledge for this need.", 404)
        try:
            need = apply_received(need.pk, pledge, s.validated_data["quantity_received"])
        except NeedError as e:
            return _err(str(e), "This pledge has already been decided.", 409)
        notify(pledge.pledger_account, "pledge_confirmed",
               title="Pledge received", body=f"“{need.title}” confirmed your pledge. Salamat!",
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
