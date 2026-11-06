"""US-B1 · badge awarding + impact aggregation.

D-S6-3: badges are event-awarded and sweep-reconciled, never counted in a column. Awarding is
an idempotent insert (uq_account_badge absorbs replays), so `award_badges_for` runs both at the
qualifying event and in the nightly catch-up sweep without ever double-awarding. Impact stats
are aggregated on read from the existing tables (§6.6) — no counters stored.

D-S6-5: `badge_earned` is in-app only (push:false). A badge is a celebration, not something to
buzz a phone for — pushing it would cheapen push for a match or a pledge.

D-S6-2: criteria are shift-agnostic — any completed Kawang-Gawa shift counts, not walks only.

IB2: a badge never gets a vote on the work that earned it. Writers call `award_after_commit`,
never `award_badges_for`: a failing award used to roll back an adoption, or answer 500 for
attendance that had already saved. The sweep repairs anything missed.
"""
import logging

from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q

from accounts.models import Account, AccountStatus
from common.analytics import emit
from listings.models import AdoptionListing, ListingStatus
from notifications.models import Notification
from notifications.service import notify
from sagip.models import RescueCase

from .models import AccountBadge, Badge, BadgeMetric, NeedPledge, PledgeStatus

logger = logging.getLogger("kupkop.badges")

# The three areas a person can help in. `areas_helped` is how many of them are above zero.
AREAS = ("shifts_completed", "rescues_helped", "pets_rehomed")


def _per_account(rows, key):
    """{account_id: count} for a queryset, grouped on `key`."""
    return {r[key]: r["n"] for r in rows.values(key).annotate(n=Count("pk"))}


def impact_counts_many(accounts):
    """The four impact aggregates for every account in `accounts`, in a fixed number of queries
    however many there are (IB23), keyed by account pk. Computed on read from existing tables
    (§6.6). The one counting path: `impact_counts` is this for a single account, and the
    completed-shift count is US-V5's `reliability_for_many`, not a second query for the same fact."""
    from volunteer.reliability import reliability_for_many
    ids = {getattr(a, "pk", a) for a in accounts}
    if not ids:
        return {}
    shifts = reliability_for_many(ids)
    # D13 · a rescue of your own report, or an animal "placed" with yourself, is real work but
    # not a badge: otherwise report → claim → resolve mints one in a minute.
    # IQ3 · and it leaves evidence (an outcome note or photo, or an adoption through Kupkop that
    # resolved it) and was not taken down: one call from claimed to resolved is not a rescue.
    adopted_through_kupkop = (AdoptionListing.objects
                              .filter(source_report_id=OuterRef("report_id"),
                                      status=ListingStatus.ADOPTED,
                                      adopted_by_account__isnull=False)
                              .exclude(adopted_by_account_id=OuterRef("claimed_by_account_id")))
    rescues = _per_account(
        RescueCase.objects.filter(claimed_by_account_id__in=ids, resolved_at__isnull=False,
                                  report__hidden_at__isnull=True)
        .exclude(report__reporter_account_id=F("claimed_by_account_id"))
        .filter(~Q(outcome_notes="") | ~Q(outcome_photo_url="") | Exists(adopted_through_kupkop)),
        "claimed_by_account_id")
    # IQ2 · a rehome has a second party: `mark-adopted` ("found a home outside Kupkop") records
    # no adopter, so it earns nothing.
    rehomed = _per_account(
        AdoptionListing.objects.filter(posted_by_id__in=ids, status=ListingStatus.ADOPTED,
                                       adopted_by_account__isnull=False)
        .exclude(adopted_by_account_id=F("posted_by_id")), "posted_by_id")
    pledges = _per_account(
        NeedPledge.objects.filter(pledger_account_id__in=ids, status=PledgeStatus.DELIVERED),
        "pledger_account_id")
    return {pk: {"shifts_completed": shifts[pk]["shifts_completed"],
                 "rescues_helped": rescues.get(pk, 0),
                 "pets_rehomed": rehomed.get(pk, 0),
                 "pledges_delivered": pledges.get(pk, 0)} for pk in ids}


def impact_counts(account):
    return impact_counts_many([account])[account.pk]


def metric_value(counts, metric):
    """How far `counts` has got on `metric`, or None for a metric nothing here knows (a row
    written ahead of its code, or a typo): such a badge is skipped, never an error."""
    if metric == BadgeMetric.AREAS:
        return sum(1 for area in AREAS if counts[area] >= 1)
    return counts.get(metric) if metric in BadgeMetric.values else None


def badge_progress(badge, counts):
    """{current, target} for a badge with a rule, else None. `current` is honest, so it can pass
    `target`: a held badge whose stat has since fallen reads below it, and the screen clamps."""
    current = metric_value(counts, badge.metric) if badge.metric and badge.threshold else None
    if current is None:
        return None
    return {"current": current, "target": badge.threshold}


def earned_codes(counts, badges):
    """The codes in `badges` that `counts` has reached: the rule is the row's own (IB5)."""
    earned = set()
    for badge in badges:
        progress = badge_progress(badge, counts)
        if progress and progress["current"] >= progress["target"]:
            earned.add(badge.pk)
    return earned


def award_badges_for(account, source="sweep"):
    """Award any newly-earned badges to `account`, idempotently. Returns the list of newly
    awarded badge_codes; fires one in-app `badge_earned` per new badge (D-S6-5, no push).

    `source` is where the award came from: `shift`, `rescue`, `rehome` or `sweep`. A sweep award
    is a hook that missed, so the event is how a hook that stopped working gets noticed (IB18).

    IB3 · a badge and its notice land together or not at all: a badge that outlived a failed
    notice was held, so no rerun could ever tell its owner. One failure stops only that badge.
    """
    # IB17 · the caller's copy of the account may be stale (the sweep reads status once per batch),
    # so a deleted or suspended account is re-checked here, where the award is decided.
    if not Account.objects.filter(pk=account.pk, status=AccountStatus.ACTIVE).exists():
        return []
    # The catalog is the rules, so a badge that isn't seeded can't be earned, and an absent
    # catalog is a no-op (never an FK error).
    badges = {b.pk: b for b in Badge.objects.filter(retired_at__isnull=True)}
    earned = earned_codes(impact_counts(account), badges.values())
    have = set(AccountBadge.objects.filter(account=account).values_list("badge_id", flat=True))
    new = sorted(earned - have)
    if len(new) > 1:
        # IB27 · several at once (a catalog rollout, or a backfill) is one notice, not one each. If the batch fails
        # for any reason it falls back to one at a time, so a single bad badge still stops only itself (IB3).
        awarded = _award_together(account, new, badges)
        if awarded is not None:
            for code in awarded:
                emit("badge_earned", badge_code=code, source=source)
            return awarded
    awarded = []
    for code in new:
        try:
            with transaction.atomic():
                # A concurrent award is absorbed by uq_account_badge; only the real insert notifies.
                _, created = AccountBadge.objects.get_or_create(account=account, badge_id=code)
                if not created:
                    continue
                notify(account, "badge_earned", title="New badge!",
                       body=f"You earned “{badges[code].name}”.", data={"badge_code": code})
        except Exception:
            logger.exception("badge %s not awarded to %s", code, account.pk)
            continue
        awarded.append(code)
        emit("badge_earned", badge_code=code, source=source)
    return awarded


def _award_together(account, codes, badges):
    """Insert every badge in `codes` and tell `account` once. Returns the codes newly inserted, or None when anything
    went wrong (nothing is kept then). A badge a concurrent award already inserted is left out of the notice."""
    try:
        with transaction.atomic():
            created = [code for code in codes
                       if AccountBadge.objects.get_or_create(account=account, badge_id=code)[1]]
            if len(created) > 1:
                names = ", ".join(f"“{badges[code].name}”" for code in created)
                notify(account, "badge_earned", title="New badges!", body=f"You earned {names}.",
                       data={"badge_codes": created})
            elif created:
                notify(account, "badge_earned", title="New badge!",
                       body=f"You earned “{badges[created[0]].name}”.", data={"badge_code": created[0]})
    except Exception:
        logger.exception("badges %s not awarded together to %s", codes, account.pk)
        return None
    return created


def award_after_commit(account, source):
    """What a qualifying writer calls (IB2). Runs once the writer's transaction commits, so a
    rollback awards nothing and no phantom `badge_earned` is counted, and it never raises."""
    def run():
        try:
            award_badges_for(account, source=source)
        except Exception:
            logger.exception("badge award failed for %s", account.pk)
    transaction.on_commit(run)


def _take_back_from_shared_notices(account, revoked):
    """IB27 · a notice for badges won together names several; take `revoked` out of it, and drop the notice when none are
    left. What remains is worded as the notice for those badges would have been."""
    if not revoked:
        return
    for note in Notification.objects.filter(account=account, type="badge_earned",
                                            data__has_key="badge_codes"):
        left = [code for code in note.data["badge_codes"] if code not in revoked]
        if len(left) == len(note.data["badge_codes"]):
            continue
        if not left:
            note.delete()
            continue
        names = {b.pk: b.name for b in Badge.objects.filter(pk__in=left)}
        if len(left) == 1:
            note.title, note.data = "New badge!", {"badge_code": left[0]}
            note.body = f"You earned “{names[left[0]]}”."
        else:
            note.data = {"badge_codes": left}
            note.body = "You earned " + ", ".join(f"“{names[c]}”" for c in left) + "."
        note.save(update_fields=["title", "body", "data"])


def reconcile_shift_badges(account):
    """IB1 / IQ1 · take back the shift badges `account` no longer qualifies for, and their
    notices, silently. Call inside the transaction that lowered the count, so a badge can't
    outlive its shift. Returns the revoked badge_codes.

    Shift badges only, and Bayani only when no shift is left: D13's "badges already awarded are
    kept" still covers rescue badges, and a kept Bayani must not be re-judged on those legs.
    """
    from volunteer.reliability import reliability_for
    shifts = reliability_for(account)["shifts_completed"]
    gone = Q(metric=BadgeMetric.SHIFTS, threshold__gt=shifts)
    if shifts < 1:
        gone |= Q(metric=BadgeMetric.AREAS)
    held = AccountBadge.objects.filter(account=account,
                                       badge__in=Badge.objects.filter(gone))
    revoked = sorted(held.values_list("badge_id", flat=True))
    held.delete()
    Notification.objects.filter(account=account, type="badge_earned",
                                data__badge_code__in=revoked).delete()
    _take_back_from_shared_notices(account, revoked)
    for code in revoked:
        transaction.on_commit(lambda code=code: emit("badge_revoked", badge_code=code,
                                                     reason="attendance_undo"))
    return revoked


