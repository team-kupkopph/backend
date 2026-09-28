"""US-V5 · the volunteer reliability signal — derived, never stored.

No counter column exists and none should be added (consistent with §6.7's
consecutive-withdrawal rule and the existing "derived from the no_show count" wording).

⚠️ D-S5-2 — aggregate only, in BOTH directions. This returns three integers and two
booleans and nothing else: never which shelters, never dates, never a per-org breakdown.
The count is global across Kupkop; the decision to approve stays each shelter's. The
volunteer sees the same numbers on their own history.
"""
from django.db.models import Count, Max

from .models import SignupStatus, VolunteerSignup

# D-S5-4 · "Reliable" requires a floor of completed shifts AND a clean current run — a
# no-show suspends Reliable even for a volunteer who cleared the floor long ago, until the
# next completed shift resets the streak. A brand-new volunteer reads "New volunteer", never
# Reliable — an unearned trust signal is worse than none. 3 matches the app's other tolerance
# numbers (3 consecutive no-shows, 3rd withdrawn inquiry).
RELIABLE_MIN_COMPLETED = 3

# 3 consecutive no-shows, reset by any completed shift (§6.5, read as consecutive).
REAPPROVAL_THRESHOLD = 3


def reliability_for(account):
    """Aggregate reliability for `account`. Returns exactly:

        {shifts_completed, no_shows, consecutive_no_shows, needs_reapproval, is_reliable}

    ⚠️ The consecutive run is computed over the SHIFT's `starts_at`, not the signup's
    `created_at`: a volunteer can book a far-future shift before a near one, and attendance
    is a fact about when the shift happened. Ordering by creation silently produces wrong
    flags.
    """
    rows = VolunteerSignup.objects.filter(volunteer_account=account)
    completed = rows.filter(status=SignupStatus.COMPLETED)
    shifts_completed = completed.count()
    no_shows = rows.filter(status=SignupStatus.NO_SHOW).count()

    last_completed_at = completed.aggregate(m=Max("shift__starts_at"))["m"]
    run_qs = rows.filter(status=SignupStatus.NO_SHOW)
    if last_completed_at is not None:
        run_qs = run_qs.filter(shift__starts_at__gt=last_completed_at)
    consecutive = run_qs.count()

    return _block(shifts_completed, no_shows, consecutive)


def _block(shifts_completed, no_shows, consecutive):
    return {
        "shifts_completed": shifts_completed,
        "no_shows": no_shows,
        "consecutive_no_shows": consecutive,
        "needs_reapproval": consecutive >= REAPPROVAL_THRESHOLD,
        "is_reliable": shifts_completed >= RELIABLE_MIN_COMPLETED and consecutive == 0,
    }


def reliability_for_many(accounts):
    """Batched `reliability_for` for a set of accounts — same block per account, keyed by
    account pk, in a bounded 2 queries regardless of how many accounts are passed (the
    requests endpoint's N pending volunteers would otherwise cost ~4N).

    The consecutive run is still measured over the SHIFT's `starts_at` per the invariant in
    `reliability_for`: no-shows are counted only when they occur after each volunteer's own
    latest completed shift. An empty `accounts` returns an empty map.
    """
    ids = {getattr(a, "pk", a) for a in accounts}
    if not ids:
        return {}

    completed = (VolunteerSignup.objects
                 .filter(volunteer_account_id__in=ids, status=SignupStatus.COMPLETED)
                 .values("volunteer_account_id")
                 .annotate(cnt=Count("pk"), last=Max("shift__starts_at")))
    completed_by = {r["volunteer_account_id"]: r for r in completed}

    # All no-show rows for these accounts, one query; the consecutive run and the total
    # no-show count are both derived from this list in Python against each account's own
    # latest completed shift time.
    no_show_rows = (VolunteerSignup.objects
                    .filter(volunteer_account_id__in=ids, status=SignupStatus.NO_SHOW)
                    .values_list("volunteer_account_id", "shift__starts_at"))

    no_shows = {aid: 0 for aid in ids}
    consecutive = {aid: 0 for aid in ids}
    for aid, starts_at in no_show_rows:
        no_shows[aid] += 1
        last = completed_by.get(aid, {}).get("last")
        if last is None or starts_at > last:
            consecutive[aid] += 1

    return {aid: _block(completed_by.get(aid, {}).get("cnt", 0),
                        no_shows[aid], consecutive[aid]) for aid in ids}
