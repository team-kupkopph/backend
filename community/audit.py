"""A read-only audit of pledges stranded by accounts deleted before backend#110.

It asks the same question the departure settling answers (`accounts/lifecycle._settle_commitments`), so
it finds exactly what that settling would have changed had it existed when the account was deleted:

  * a `pledged` pledge whose DONOR account is deleted (it would have ended as `donor_left`);
  * an `open` need whose SHELTER account is deleted, or a `fulfilled` one still holding a waiting late
    pledge (it would have gone through `complete_need`);
  * the `pledged` pledges on those needs (they would have ended as `need_closed`).

A pledge can be in two groups (donor and shelter both gone), so the total is a DISTINCT count.

Ids and dates only: no name, email or need title, because this output gets pasted into tickets. It never
writes. `scanned` is the denominator: a zero with no denominator cannot be told from a query that matched
nothing by mistake, which is how every audit in this project has under-reported at least once.
"""
from django.db.models import Q

from accounts.models import Account, AccountStatus
from community.models import NeedPledge, NeedStatus, PledgeStatus, ShelterNeed

DEFAULT_LIMIT = 20


def _pledge_rows(qs, limit):
    return [{"pledge_id": str(pk), "need_id": str(need_id), "donor_account_id": str(donor_id),
             "created_at": created.isoformat()}
            for pk, need_id, donor_id, created in
            qs.order_by("created_at", "pk").values_list("pk", "need_id", "pledger_account_id", "created_at")[:limit]]


def _need_rows(qs, limit):
    return [{"need_id": str(pk), "shelter_account_id": str(shelter_id), "status": status,
             "created_at": created.isoformat()}
            for pk, shelter_id, status, created in
            qs.order_by("created_at", "pk").values_list("pk", "shelter_account_id", "status", "created_at")[:limit]]


def stranded_pledge_report(limit=DEFAULT_LIMIT):
    deleted = Q(status=AccountStatus.DELETED)
    waiting = NeedPledge.objects.filter(status=PledgeStatus.PLEDGED)

    donor_pledges = waiting.filter(pledger_account__in=Account.objects.filter(deleted))
    gone_shelters = Account.objects.filter(deleted)
    shelter_needs = (ShelterNeed.objects.filter(shelter_account__in=gone_shelters)
                     .filter(Q(status=NeedStatus.OPEN)
                             | Q(status=NeedStatus.FULFILLED, pledges__status=PledgeStatus.PLEDGED))
                     .distinct())
    shelter_need_pledges = waiting.filter(need__shelter_account__in=gone_shelters)

    to_settle = (NeedPledge.objects.filter(pk__in=donor_pledges.values("pk"))
                 | NeedPledge.objects.filter(pk__in=shelter_need_pledges.values("pk"))).count()
    return {
        "scanned": {
            "deleted_accounts": Account.objects.filter(deleted).count(),
            "waiting_pledges": waiting.count(),
            "open_or_fulfilled_needs": ShelterNeed.objects.filter(
                status__in=[NeedStatus.OPEN, NeedStatus.FULFILLED]).count(),
        },
        "donor_pledges": {"count": donor_pledges.count(), "rows": _pledge_rows(donor_pledges, limit)},
        "shelter_needs": {"count": shelter_needs.count(), "rows": _need_rows(shelter_needs, limit)},
        "shelter_need_pledges": {"count": shelter_need_pledges.count(),
                                 "rows": _pledge_rows(shelter_need_pledges, limit)},
        "pledges_to_settle": to_settle,
    }
