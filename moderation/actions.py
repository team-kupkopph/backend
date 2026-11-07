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

from moderation import offenders, telemetry
from moderation.models import DECIDABLE, FlagStatus, ModerationFlag, ReasonCode
from moderation.notices import removal_reason, tell_reporters
from moderation.targets import REGISTRY


class ModerationError(Exception):
    """A decision that cannot be applied as asked."""


NOTE_MAX = 2000


class DecisionInvalid(ModerationError):
    """The decision is missing something it needs. `code` is the machine word the console keys on."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


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


def _close_siblings(flag, reviewer, reason_code=""):
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
    ModerationFlag.objects.filter(pk__in=ids).update(status=FlagStatus.ACTIONED, reviewed_by=reviewer, reviewed_at=now,
                                                     decision_reason_code=reason_code)
    return list(ModerationFlag.objects.filter(pk__in=ids).select_related("reporter_account"))


@transaction.atomic
def resolve_flag(flag, reviewer, status, *, reason_code="", note=""):
    """Move a flag to `status`, stamping the reviewer, and apply the status's side effects.

    Review step 8 (MO8) · ACTIONING needs a reason: a category (`reason_code`, one of ReasonCode) the poster is told as a fixed
    sentence, and a note when the category is `other`. Restores already demanded a reason; the action that hurts someone did not.
    The note is internal and is kept on the flag. Dismissing and reviewing take an optional note and no category.

    Returns (flag, side_effects) where side_effects names what else changed, so a caller can
    tell the reviewer "and the story was hidden" rather than leaving it invisible.
    """
    if status not in {FlagStatus.REVIEWED, FlagStatus.ACTIONED, FlagStatus.DISMISSED}:
        raise ModerationError(f"Unknown flag status: {status!r}")
    note = (note or "").strip()
    if len(note) > NOTE_MAX:
        raise DecisionInvalid("note_too_long", f"The note can be at most {NOTE_MAX} characters.")
    if status == FlagStatus.ACTIONED:
        if reason_code and reason_code not in ReasonCode.values:
            raise DecisionInvalid("invalid_reason_code", "That is not a reason we know.")
    else:
        reason_code = ""
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
    changed, owner = False, None

    if status == FlagStatus.ACTIONED:
        spec = REGISTRY.get(flag.target_type)
        if spec is None or not spec.supported:
            raise TargetUnavailable("unsupported")
        target = spec.fetch(flag.target_id)
        if target is None:
            raise TargetUnavailable("gone")
        # A takedown that is about to change something needs a reason (MO8). A record-only action (an account, an inquiry) has no
        # poster to tell, and a target already held down has nothing to explain, so neither is made to give one.
        if spec.take_down is not None and not (spec.already_done and spec.already_done(target) is not None):
            if not reason_code:
                raise DecisionInvalid("reason_required", "Choose why this is being taken down.")
            if reason_code == ReasonCode.OTHER and not note:
                raise DecisionInvalid("note_required", "Say what is wrong when the reason is something else.")
        changed = spec.take_down is not None and spec.take_down(target, reviewer, removal_reason(reason_code))
        if changed:
            side_effects[spec.effect] = str(flag.target_id)
            owner = spec.person(target) if spec.person is not None else None
        # Review step 5 (MO4) · a decision is about the target. Once it is held down (just now, or already), every other
        # live flag about it is moot, and leaving them open lets a stale one hide it again after a restore. A type that
        # takes nothing down (an account flag) settles nothing for the others, so they stay. Dismissing and reviewing
        # never reach here: another reporter may have a different concern (MQ4).
        held = changed or (spec.take_down is not None and spec.already_done is not None
                           and spec.already_done(target) is not None)
        if held:
            closed = _close_siblings(flag, reviewer, reason_code)
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
    flag.decision_reason_code, flag.decision_note = reason_code, note
    fields = ["status", "reviewed_by", "reviewed_at", "decision_reason_code", "decision_note"]
    if flag.first_reviewed_at is None:        # step 19 · the first look is kept, whoever decides after
        flag.first_reviewed_at, flag.first_reviewed_by = now, reviewer
        fields += ["first_reviewed_at", "first_reviewed_by"]
    flag.save(update_fields=fields)
    if status != FlagStatus.REVIEWED:          # `reviewed` is a working state; the reporter hears when it is decided
        tell_reporters([flag])
    telemetry.flag_decided(flag, status, changed, first_response=flag.first_reviewed_at == now)
    if changed and owner is not None:
        # Review step 12 (MQ6) · a takedown can be the third in 90 days; if so a human is asked to look at the person, never suspended.
        offenders.escalate_if_repeat_takedown(owner, now)
    return flag, side_effects
