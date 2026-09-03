"""US-W1 · the single writer for a shelter need's received total and status.

D-S6-7: `quantity_received` and the `open -> fulfilled` flip change ONLY here, under a row
lock, so two staff confirming pledges at once can't lost-update the total. Mirrors
`volunteer/status.py::set_signup_status` and `listings/stages.py::set_stage_state` — one door,
so no view touches the columns directly.
"""
from django.db import transaction

from .models import NeedPledge, NeedStatus, PledgeStatus, ShelterNeed


class NeedError(ValueError):
    """A received-confirm that can't be applied (e.g. the pledge is already decided)."""


@transaction.atomic
def apply_received(need_id, pledge, quantity_received):
    """Mark `pledge` delivered and add `quantity_received` to the need's running total,
    flipping the need to `fulfilled` once the target is met. The need row is locked for the
    duration so concurrent confirms serialize and sum instead of clobbering each other.

    Returns the refreshed need. Raises `NeedError` if the pledge is not still `pledged`.
    """
    need = ShelterNeed.objects.select_for_update().get(pk=need_id)
    if pledge.status != PledgeStatus.PLEDGED:
        raise NeedError("pledge_decided")

    pledge.status = PledgeStatus.DELIVERED
    pledge.save(update_fields=["status"])

    need.quantity_received += quantity_received
    if need.quantity_received >= need.quantity_needed and need.status == NeedStatus.OPEN:
        need.status = NeedStatus.FULFILLED
        need.save(update_fields=["quantity_received", "status"])
    else:
        need.save(update_fields=["quantity_received"])
    return need
