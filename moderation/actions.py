"""Moderation decisions as a service (Sprint 10 US-M2).

⚠️ WHY THIS FILE EXISTS. Until now the only implementation of "resolve a flag" lived inside
`moderation/admin.py::ModerationFlagAdmin._resolve` — including US-T3 / D-S6-4, where actioning
a STORY flag hides the story. Sprint 10 US-X2 deletes the Django admin route, and behaviour
that lives only in the surface being deleted goes with it. Nobody would notice until a story
flag was actioned in the console and the story stayed visible.

So the logic moves here and BOTH surfaces call it. This mirrors `verifications/review.py`,
which the verification queue already delegates to for exactly the same reason: one writer, and
the decision survives the UI that happened to invoke it first.
"""
from django.db import transaction
from django.utils import timezone

from moderation.models import FlagStatus, FlagTarget

# Statuses a flag can still be moved from. `open` and `reviewed` are working states; `actioned`
# and `dismissed` are terminal, and a second decision would overwrite the first reviewer's
# stamp with no record that it happened.
DECIDABLE = {FlagStatus.OPEN, FlagStatus.REVIEWED}


class ModerationError(Exception):
    """A decision that cannot be applied as asked."""


@transaction.atomic
def resolve_flag(flag, reviewer, status, notes=""):
    """Move a flag to `status`, stamping the reviewer, and apply the status's side effects.

    Returns (flag, side_effects) where side_effects names what else changed, so a caller can
    tell the reviewer "and the story was hidden" rather than leaving it invisible.
    """
    if status not in {FlagStatus.REVIEWED, FlagStatus.ACTIONED, FlagStatus.DISMISSED}:
        raise ModerationError(f"Unknown flag status: {status!r}")
    if reviewer is None:
        # The same contract accounts/staff.py states: never silently stamp an anonymous
        # decision. The console refuses a token without a StaffProfile, so this is the
        # defence-in-depth copy rather than the only check.
        raise ModerationError("A moderation decision must be attributed to a reviewer.")

    side_effects = {}

    if status == FlagStatus.ACTIONED and flag.target_type == FlagTarget.STORY:
        # US-T3 / D-S6-4 · hiding, NOT deletion. The row and its photos survive, the feed
        # excludes it, and the author sees a "hidden by moderation" state rather than a
        # vanished post. The flag stays as the audit trail.
        from community.models import StoryPost, StoryStatus
        hidden = StoryPost.objects.filter(pk=flag.target_id).update(status=StoryStatus.HIDDEN)
        if hidden:
            side_effects["story_hidden"] = str(flag.target_id)

    # ⚠️ Actioning a flag does NOT suspend an account, even for target_type='account'.
    # Suspension has its own side effects (token revocation, US-E2) and its own audit trail.
    # A flag resolved as `actioned` records the JUDGEMENT; the action is taken separately and
    # deliberately, by someone who chose to take it.

    flag.status = status
    flag.reviewed_by = reviewer
    flag.reviewed_at = timezone.now()
    flag.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    return flag, side_effects
