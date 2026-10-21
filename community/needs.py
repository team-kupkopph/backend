"""US-W1 · the single writer for a shelter need's pledges, received total and status.

D-S6-7: `quantity_received` and the `open -> fulfilled` flip change ONLY here, under a row
lock, so two staff confirming pledges at once can't lost-update the total. Mirrors
`volunteer/status.py::set_signup_status` and `listings/stages.py::set_stage_state` — one door,
so no view touches the columns directly.

Lock order, always: the `shelter_need` row first (`select_for_update`), THEN the pledge row
(`select_for_update`, re-read). A pledge is never locked without its need, and a pledge's
status is only ever checked on the row re-read under both locks — never on an object loaded
before the lock (that was the double-count: AW1).
"""
from django.db import transaction

from .models import NeedPledge, NeedStatus, PledgeStatus, ShelterNeed

MAX_QTY = 100_000          # per need target, per pledge, and per received-confirm
MAX_TOTAL = 2_000_000_000  # running `quantity_received` ceiling (int4 max is 2_147_483_647)


class NeedError(ValueError):
    """A wishlist transition that can't be applied. `code` is the machine string the view
    maps to a status (`not_found`, `need_not_open`, `pledge_decided`, `total_overflow`);
    `message` optionally overrides the view's default wording for that code."""

    def __init__(self, code, message=""):
        super().__init__(code)
        self.code = code
        self.message = message


def _lock_need(need_id):
    need = ShelterNeed.objects.select_for_update().filter(pk=need_id).first()
    if need is None:
        raise NeedError("not_found", "No such need.")
    return need


@transaction.atomic
def create_pledge(need_id, pledger, quantity):
    """Record a `pledged` pledge by `pledger` against the need — only while it is `open`.

    The need row is locked and its status re-read, so a pledge can't slip in after a close
    (or a fulfil) that committed first. Over-pledging an open need is allowed."""
    need = _lock_need(need_id)
    if need.status != NeedStatus.OPEN:
        raise NeedError("need_not_open")
    return NeedPledge.objects.create(need=need, pledger_account=pledger, quantity=quantity)


@transaction.atomic
def cancel_pledge(pledge_id, pledger):
    """The pledger withdraws their own still-`pledged` pledge. A decided pledge is a recorded
    fact: `pledge_decided`. Someone else's (or no) pledge: `not_found`."""
    # The need id is needed to take the locks in order; it never changes, so read it unlocked.
    need_id = (NeedPledge.objects.filter(pk=pledge_id, pledger_account=pledger)
               .values_list("need_id", flat=True).first())
    if need_id is None:
        raise NeedError("not_found", "No such pledge.")
    _lock_need(need_id)
    pledge = (NeedPledge.objects.select_for_update()
              .filter(pk=pledge_id, need_id=need_id, pledger_account=pledger).first())
    if pledge is None:
        raise NeedError("not_found", "No such pledge.")
    if pledge.status != PledgeStatus.PLEDGED:
        raise NeedError("pledge_decided")
    pledge.status = PledgeStatus.CANCELLED
    pledge.save(update_fields=["status"])
    return pledge


@transaction.atomic
def apply_received(need_id, pledge_id, quantity_received):
    """Mark the pledge delivered and add `quantity_received` to the need's running total,
    flipping the need to `fulfilled` once the target is met.

    The need is locked, then the pledge re-read under that lock, so concurrent confirms (and
    a racing cancel) serialize: each pledge is counted at most once and a cancelled pledge is
    never counted. A `closed` need refuses (`need_not_open`); a `fulfilled` one still takes a
    late delivery, since over-pledging means the pledges beyond the target may still arrive.
    The actual count may exceed the pledged quantity.

    Returns the refreshed need. Raises `NeedError`: `not_found`, `pledge_decided`,
    `need_not_open`, or `total_overflow` (the total would pass `MAX_TOTAL`).
    """
    need = _lock_need(need_id)
    pledge = (NeedPledge.objects.select_for_update()
              .filter(pk=pledge_id, need_id=need_id).first())
    if pledge is None:
        raise NeedError("not_found", "No such pledge for this need.")
    if pledge.status != PledgeStatus.PLEDGED:
        raise NeedError("pledge_decided")
    if need.status == NeedStatus.CLOSED:
        raise NeedError("need_not_open")
    if need.quantity_received + quantity_received > MAX_TOTAL:
        raise NeedError("total_overflow")

    pledge.status = PledgeStatus.DELIVERED
    pledge.save(update_fields=["status"])

    need.quantity_received += quantity_received
    if need.quantity_received >= need.quantity_needed and need.status == NeedStatus.OPEN:
        need.status = NeedStatus.FULFILLED
        need.save(update_fields=["quantity_received", "status"])
    else:
        need.save(update_fields=["quantity_received"])
    return need
