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
from django.utils import timezone

from accounts.models import Account, AccountStatus
from verifications.models import VerificationRequest

from .models import NeedPledge, NeedStatus, PledgeStatus, ShelterNeed

MAX_QTY = 100_000          # per need target, per pledge, and per received-confirm
MAX_TOTAL = 2_000_000_000  # running `quantity_received` ceiling (int4 max is 2_147_483_647)


class CancelReason:
    """Who ended a pledge: `donor` withdrew it; `need_closed` is the shelter's Mark complete (or
    the shelter leaving Kupkop); `donor_left` is the donor deleting their account (T4)."""
    DONOR = "donor"
    NEED_CLOSED = "need_closed"
    DONOR_LEFT = "donor_left"


EDITABLE_FIELDS = ("title", "description", "quantity_needed", "category", "unit")


class NeedError(ValueError):
    """A wishlist transition that can't be applied. `code` is the machine string the view
    maps to a status (`not_found`, `forbidden`, `need_not_open`, `pledge_decided`,
    `total_overflow`, `own_need`, `below_received`);
    `message` optionally overrides the view's default wording for that code, and `details`
    is the machine-readable extra the envelope carries (e.g. `below_received`)."""

    def __init__(self, code, message="", details=None):
        super().__init__(code)
        self.code = code
        self.message = message
        self.details = details


def live_shelter_ids():
    """AW2/AW31 · the shelters whose wishlist the public may see and pledge to: an `active`
    shelter account with an approved `shelter_org` verification. A suspended or deleted
    shelter, or one still awaiting approval, is not live."""
    approved = VerificationRequest.objects.filter(type="shelter_org", status="approved")
    return (Account.objects
            .filter(account_type="shelter", status=AccountStatus.ACTIVE,
                    pk__in=approved.values("account_id"))
            .values_list("pk", flat=True))


def live_needs():
    """Every public read of needs, and `create_pledge`, starts from here. The owning shelter's
    own view of its wishlist is the one deliberate exception (see `ShelterNeedsView.get`)."""
    return ShelterNeed.objects.filter(shelter_account_id__in=live_shelter_ids())


def _lock_need(need_id):
    need = ShelterNeed.objects.select_for_update().filter(pk=need_id).first()
    if need is None:
        raise NeedError("not_found", "No such need.")
    return need


def owned_need_id(need_id, owner, forbidden_message):
    """Ownership gate for the shelter-side writers, run BEFORE any lock is taken: a stranger
    probing someone else's need must not queue on its row lock. The owner never changes, so
    reading it unlocked is safe. Unknown need: `not_found`; someone else's: `forbidden`."""
    owner_id = (ShelterNeed.objects.filter(pk=need_id)
                .values_list("shelter_account_id", flat=True).first())
    if owner_id is None:
        raise NeedError("not_found", "No such need.")
    if owner_id != owner.pk:
        raise NeedError("forbidden", forbidden_message)
    return need_id


@transaction.atomic
def create_pledge(need_id, pledger, quantity, note=""):
    """Record a `pledged` pledge by `pledger` against the need — only while it is `open`.

    The need row is locked and its status re-read, so a pledge can't slip in after a close
    (or a fulfil) that committed first. Over-pledging an open need is allowed.

    Trust gates, in this order: a shelter can't pledge to its own need (`own_need`) — checked
    first so an unapproved shelter pledging to its own wishlist is told why, not "not found";
    then the need must belong to a live shelter (`not_found`, so a suspended shelter's wishlist
    is indistinguishable from a missing one)."""
    need = _lock_need(need_id)
    if pledger.pk == need.shelter_account_id:
        raise NeedError("own_need")
    if not live_needs().filter(pk=need.pk).exists():
        raise NeedError("not_found", "No such need.")
    if need.status != NeedStatus.OPEN:
        raise NeedError("need_not_open")
    return NeedPledge.objects.create(need=need, pledger_account=pledger, quantity=quantity,
                                     note=note)


@transaction.atomic
def cancel_pledge(pledge_id, pledger, reason=CancelReason.DONOR):
    """The pledger withdraws their own still-`pledged` pledge. A decided pledge is a recorded
    fact: `pledge_decided`. Someone else's (or no) pledge: `not_found`. `reason` is `donor` for a
    withdrawal and `donor_left` when the pledger is deleting their account."""
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
    pledge.cancel_reason = reason
    pledge.decided_at = timezone.now()
    pledge.save(update_fields=["status", "cancel_reason", "decided_at"])
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

    now = timezone.now()
    pledge.status = PledgeStatus.DELIVERED
    pledge.quantity_received = quantity_received
    pledge.decided_at = now
    pledge.save(update_fields=["status", "quantity_received", "decided_at"])

    need.quantity_received += quantity_received
    if need.quantity_received >= need.quantity_needed and need.status == NeedStatus.OPEN:
        need.status = NeedStatus.FULFILLED
        need.fulfilled_at = now
        need.save(update_fields=["quantity_received", "status", "fulfilled_at"])
    else:
        need.save(update_fields=["quantity_received"])
    return need


@transaction.atomic
def edit_need(need_id, owner, fields):
    """The owning shelter edits an `open` need. Takes the need lock like every other writer, so
    a confirm that committed first is never overwritten by a stale full-row save (the lost
    update), and writes only the columns that changed (`update_fields`).

    `fields` is a dict drawn from `EDITABLE_FIELDS`. Raises `NeedError`: `not_found`,
    `forbidden`, `need_not_open` (only an open need is editable), or `below_received` (the new
    target is under what has already arrived; `details` carries the received count). Lowering
    the target to exactly what has arrived flips the need to `fulfilled` (stamping
    `fulfilled_at`), inside the same lock."""
    owned_need_id(need_id, owner, "Only the owning shelter can edit this need.")
    need = _lock_need(need_id)
    if need.status != NeedStatus.OPEN:
        raise NeedError("need_not_open", "Only an open need can be edited.")
    target = fields.get("quantity_needed")
    if target is not None and target < need.quantity_received:
        raise NeedError(
            "below_received",
            f"{need.quantity_received} already received — the target can't go lower.",
            {"quantity_received": need.quantity_received})
    changed = [f for f in EDITABLE_FIELDS if f in fields]
    for f in changed:
        setattr(need, f, fields[f])
    if need.quantity_received >= need.quantity_needed:
        # Same rule as `apply_received`'s flip: an open need at 100% is fulfilled.
        need.status = NeedStatus.FULFILLED
        need.fulfilled_at = timezone.now()
        changed += ["status", "fulfilled_at"]
    if changed:
        need.save(update_fields=changed)
    return need


@transaction.atomic
def complete_need(need_id):
    """Mark complete: resolve an `open` need to `fulfilled` (received >= needed, stamping
    `fulfilled_at`) or `closed`, and cancel every still-`pledged` pledge on it
    (`cancel_reason="need_closed"`). Delivered and already-cancelled pledges are facts and stay.

    Returns `(need, cancelled_pledges)` so the view can tell each donor AFTER this transaction
    commits. On a `fulfilled` need it settles the pledges still waiting (a fulfilled need takes
    late deliveries, R1, so they could otherwise wait "to confirm" for ever) and leaves the need
    fulfilled, `fulfilled_at` as it was. On a `closed` need it changes nothing and returns
    `(need, [])`. A repeat call finds nothing left `pledged`, which is what makes it, or a double
    tap, silent. The need lock is taken first, so a donor's cancel or a shelter's confirm racing
    this one serialize: each pledge ends exactly one way."""
    need = _lock_need(need_id)
    if need.status == NeedStatus.CLOSED:
        return need, []
    now = timezone.now()
    # `of=("self",)`: without it Postgres also row-locks the joined `account` rows, a third
    # table no other writer locks, in pledge order — two completes sharing donors in opposite
    # order could deadlock, and the donors' own writes would block.
    cancelled = list(NeedPledge.objects.select_for_update(of=("self",))
                     .filter(need=need, status=PledgeStatus.PLEDGED)
                     .select_related("pledger_account").order_by("created_at"))
    if cancelled:
        NeedPledge.objects.filter(pk__in=[p.pk for p in cancelled]).update(
            status=PledgeStatus.CANCELLED, cancel_reason=CancelReason.NEED_CLOSED, decided_at=now)
        for p in cancelled:
            p.status = PledgeStatus.CANCELLED
            p.cancel_reason = CancelReason.NEED_CLOSED
            p.decided_at = now
    if need.status == NeedStatus.FULFILLED:
        return need, cancelled
    if need.quantity_received >= need.quantity_needed:
        need.status = NeedStatus.FULFILLED
        need.fulfilled_at = now
        need.save(update_fields=["status", "fulfilled_at"])
    else:
        need.status = NeedStatus.CLOSED
        need.save(update_fields=["status"])
    return need, cancelled


def settle_departing_shelter(shelter):
    """T4 · a shelter is leaving Kupkop: do to every need of theirs what Mark complete does.

    Each `open` need, and each `fulfilled` one still holding a waiting late pledge, goes through
    `complete_need` — the same writer, so the need resolves to `fulfilled` or `closed` and every
    `pledged` pledge ends exactly one way, under the same locks. A `closed` need, a delivered
    pledge and an already-cancelled one are facts and stay.

    Returns `[(need, cancelled_pledges), ...]` for the caller to tell each donor once the delete
    commits. Runs inside the caller's transaction (`complete_need` nests in it).
    """
    from django.db.models import Q
    ids = list(ShelterNeed.objects
               .filter(shelter_account=shelter)
               .filter(Q(status=NeedStatus.OPEN)
                       | Q(status=NeedStatus.FULFILLED, pledges__status=PledgeStatus.PLEDGED))
               .order_by("created_at").values_list("pk", flat=True).distinct())
    settled = []
    for need_id in ids:
        need, cancelled = complete_need(need_id)
        if cancelled:
            settled.append((need, cancelled))
    return settled


def cancel_departing_donor_pledges(donor):
    """T4 · a donor is leaving Kupkop: end their still-`pledged` pledges as `donor_left`.

    Each goes through `cancel_pledge`, so the need lock is taken first like every other writer,
    and a pledge that was decided in the meantime (the shelter confirmed it a moment ago) is a
    fact and is skipped. Returns the pledges that were cancelled, for the caller to tell their
    shelters."""
    ids = list(NeedPledge.objects.filter(pledger_account=donor, status=PledgeStatus.PLEDGED)
               .order_by("created_at").values_list("pk", flat=True))
    cancelled = []
    for pledge_id in ids:
        try:
            cancelled.append(cancel_pledge(pledge_id, donor, reason=CancelReason.DONOR_LEFT))
        except NeedError:
            continue
    return cancelled


def settle_stranded_pledges(pledge_model, now):
    """Backlog · before the refactor closing a need did not cascade, so a `closed` need could keep
    `pledged` pledges: the donor's card still read "Pledged" with a live Cancel, and the shelter
    could never receive them. Settle them the way a close settles a pledge today (`cancelled`,
    `cancel_reason="need_closed"`), without a notification — those needs closed long ago. Pledges
    on open or fulfilled needs are left alone (a fulfilled need still takes late deliveries, R1).
    Takes the model so the data migration can pass its historical one; returns how many changed."""
    return (pledge_model.objects.filter(status="pledged", need__status="closed")
            .update(status="cancelled", cancel_reason="need_closed", decided_at=now))
