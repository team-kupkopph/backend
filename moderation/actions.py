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

from moderation.models import DECIDABLE, FlagStatus, ModerationFlag
from moderation.notices import tell_reporters
from moderation.targets import REGISTRY


class ModerationError(Exception):
    """A decision that cannot be applied as asked."""


class AlreadyDecided(ModerationError):
    """The flag was already actioned or dismissed. A decision is final: a second one would overwrite the first reviewer's
    stamp with no record that it happened (review step 18, MO16: the guard used to live in one caller, not here)."""

    def __init__(self, status):
        self.status = status
        super().__init__(f"This flag was already {status}.")


class TargetUnavailable(ModerationError):
    """`actioned` was asked of a flag whose target is gone (`state == "gone"`) or has no surface
    (`"unsupported"`). Actioning records that something was done about the target; nothing can be, so the flag
    would claim work nobody did. It can still be dismissed or marked reviewed."""

    def __init__(self, state):
        self.state = state
        super().__init__("The target no longer exists, so there is nothing to action. Dismiss the flag, or mark it reviewed."
                         if state == "gone" else
                         "This type of target has no surface yet, so there is nothing to action. Dismiss the flag, "
                         "or mark it reviewed.")


def _close_siblings(flag, reviewer):
    """Mark the other open and reviewed flags about `flag`'s target as actioned, stamped like the decision itself.

    ⚠️ `skip_locked`: another moderator deciding one of those siblings right now holds its row lock, and waiting for
    it while they wait for ours is a deadlock. A sibling someone else is deciding is theirs to decide; it is skipped,
    not closed from under them."""
    ids = list(ModerationFlag.objects.select_for_update(skip_locked=True)
               .filter(target_type=flag.target_type, target_id=flag.target_id, status__in=DECIDABLE)
               .exclude(pk=flag.pk).values_list("pk", flat=True))
    if not ids:
        return []
    now = timezone.now()
    ModerationFlag.objects.filter(pk__in=ids, first_reviewed_at__isnull=True).update(
        first_reviewed_at=now, first_reviewed_by=reviewer)
    ModerationFlag.objects.filter(pk__in=ids).update(status=FlagStatus.ACTIONED, reviewed_by=reviewer, reviewed_at=now)
    return list(ModerationFlag.objects.filter(pk__in=ids).select_related("reporter_account"))


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

    # Lock the row and check it HERE, so every caller (the console, the retired Django admin, whatever comes next) gets the
    # same guard. Two moderators on one flag is normal; without the lock the second silently overwrites the first.
    flag = ModerationFlag.objects.select_for_update().get(pk=flag.pk)
    if flag.status not in DECIDABLE:
        raise AlreadyDecided(flag.status)

    side_effects = {}

    if status == FlagStatus.ACTIONED:
        spec = REGISTRY.get(flag.target_type)
        if spec is None or not spec.supported:
            raise TargetUnavailable("unsupported")
        target = spec.fetch(flag.target_id)
        if target is None:
            raise TargetUnavailable("gone")
        changed = spec.take_down is not None and spec.take_down(target, reviewer)
        if changed:
            side_effects[spec.effect] = str(flag.target_id)
        # Review step 5 (MO4) · a decision is about the target. Once it is held down (just now, or already), every other
        # live flag about it is moot, and leaving them open lets a stale one hide it again after a restore. A type that
        # takes nothing down (an account flag) settles nothing for the others, so they stay. Dismissing and reviewing
        # never reach here: another reporter may have a different concern (MQ4).
        held = changed or (spec.take_down is not None and spec.already_done is not None
                           and spec.already_done(target) is not None)
        if held:
            closed = _close_siblings(flag, reviewer)
            if closed:
                side_effects["flags_resolved"] = str(len(closed))
                tell_reporters(closed)

    # ⚠️ Actioning a flag does NOT suspend an account, even for target_type='account'.
    # Suspension has its own side effects (token revocation, US-E2) and its own audit trail.
    # A flag resolved as `actioned` records the JUDGEMENT; the action is taken separately and
    # deliberately, by someone who chose to take it.

    now = timezone.now()
    flag.status = status
    flag.reviewed_by = reviewer
    flag.reviewed_at = now
    fields = ["status", "reviewed_by", "reviewed_at"]
    if flag.first_reviewed_at is None:        # step 19 · the first look is kept, whoever decides after
        flag.first_reviewed_at, flag.first_reviewed_by = now, reviewer
        fields += ["first_reviewed_at", "first_reviewed_by"]
    flag.save(update_fields=fields)
    if status != FlagStatus.REVIEWED:          # `reviewed` is a working state; the reporter hears when it is decided
        tell_reporters([flag])
    return flag, side_effects
