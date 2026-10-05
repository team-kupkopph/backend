"""The single writer for an AdoptionStage's state — mirrors sagip/status.py::
set_report_status exactly, for the same reason: a stage must never move without an
AdoptionStageHistory row recording who moved it and why. `adoption_stage_history` was
added specifically because the core adoption funnel had no transition log and couldn't
be backfilled — this is what keeps that from happening again.
"""
from django.db import transaction

from listings.models import AdoptionStageHistory, AdoptionStageKey, StageState


class StageError(ValueError):
    """A stage move that can't be applied — e.g. an unknown target state."""


@transaction.atomic
def set_stage_state(stage, state, by, note=""):
    """Move `stage` to `state` and log it, atomically. `by` is the acting Account
    (nullable-safe — SET_NULL on the history row if later deleted). Returns the created
    `AdoptionStageHistory` row. Raises `StageError` for a state outside `StageState`."""
    if state not in StageState.values:
        raise StageError(f"unknown stage state: {state!r}")
    stage.state = state
    if note:
        stage.note = note
    stage.save(update_fields=["state", "note", "updated_at"])
    return AdoptionStageHistory.objects.create(
        inquiry=stage.inquiry, stage_key=stage.stage_key, state=state,
        changed_by_account=by)


# AQ5 (2026-10-05) · an individual poster (a rescuer, or an owner rehoming) doesn't run home
# visits or vet clearance. Their applicants' ladders start with these two SKIPPED, and the poster
# can still un-skip either with a stage move.
INDIVIDUAL_SKIPPED_STAGES = (AdoptionStageKey.HOME_CHECK, AdoptionStageKey.VET_CLEARANCE)
INDIVIDUAL_SKIP_NOTE = "Not needed when adopting from an individual."
