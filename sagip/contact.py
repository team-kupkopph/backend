"""Who on a rescue can see whom, and how to reach them (D1 + D8, dev/sagip-build-review.md).

Owner decisions 2026-09-30. D1: a consented contact reveal after a claim, mirroring the
volunteer contact gate — each person opts in on their own row (the report, the case, the
offer), phone and email only. D8: the claimer sees who reported by default; "Report
anonymously" hides the reporter's name and forbids sharing their contact.

The field-authorization table this module enforces for `GET /reports/{id}`'s `people`:

    viewer            | sees                                         | when
    ------------------+----------------------------------------------+-----------------------------
    reporter          | the claimer                                  | a claim is active or resolved
    active claimer    | the reporter (or "anonymous"), each matched  |   (never before a claim,
                      |   helper with what they offered              |    never after it lapses)
    matched helper    | the claimer                                  |
    anyone else       | nothing — `people` is absent                 |

A `contact` key appears on an entry only when THAT person consented; otherwise it is absent,
never null (the volunteer roster's rule). A phone is shared only once verified.

C2 · owner decision 2026-10-01: consent was given for a rescue, not forever. Contact stays for
CONTACT_AFTER_RESOLVED once the rescue is resolved — long enough to settle follow-ups (vet receipts,
a helper's supplies, checking in on the animal) — then only names remain. The same window covers
the owner/finder contact on a resolved lost<->found match (`views._match_counterpart`).
"""
from django.utils import timezone

from sagip.models import OfferStatus, StrayStatus

CONTACT_AFTER_RESOLVED = timezone.timedelta(days=7)


def contact_window_open(report, case=None, now=None):
    """C2 · may consented contact still be shown for this rescue? Always while it's open; for
    CONTACT_AFTER_RESOLVED after it resolved. The resolution time is the case's `resolved_at`,
    else the report's latest `resolved` history row (a reporter's close, a confirmed match)."""
    if report.status != StrayStatus.RESOLVED:
        return True
    resolved_at = case.resolved_at if case is not None and case.resolved_at else None
    if resolved_at is None:
        row = (report.status_history.filter(status=StrayStatus.RESOLVED)
               .order_by("-changed_at").first())
        resolved_at = row.changed_at if row else None
    if resolved_at is None:
        return True
    return (now or timezone.now()) - resolved_at <= CONTACT_AFTER_RESOLVED


def contact_of(account):
    """Phone and email — exactly what the consent names. An unverified phone may not even be
    theirs, so it's withheld."""
    return {"phone": account.phone if account.phone_verified_at else None,
            "email": account.email}


def _entry(role, account, consented):
    entry = {"role": role, "display_name": account.display_name}
    if consented:
        entry["contact"] = contact_of(account)
    return entry


def people_for(report, user):
    """The `people` list for this viewer, or None when they aren't on this rescue (or it has no
    live-or-resolved claim). See the table in the module docstring."""
    if not getattr(user, "is_authenticated", False):
        return None
    case = (report.cases.filter(expired_at__isnull=True)
            .select_related("claimed_by_account").first())
    if case is None:
        return None
    open_ = contact_window_open(report, case)          # C2 · names stay, contact expires
    claimer = _entry("claimer", case.claimed_by_account, case.contact_share_consent and open_)

    if user.pk == case.claimed_by_account_id:
        people = []
        # A reporter who claimed their own report doesn't need to be shown to themselves.
        if report.reporter_account_id and report.reporter_account_id != user.pk:
            if report.is_anonymous:
                people.append({"role": "reporter", "anonymous": True})   # D8
            else:
                people.append(_entry("reporter", report.reporter_account,
                                     report.contact_share_consent and open_))
        for offer in (report.offers.filter(status=OfferStatus.MATCHED)
                      .select_related("account").order_by("created_at")):
            helper = _entry("helper", offer.account, offer.contact_share_consent and open_)
            helper.update(offer_type=offer.offer_type, note=offer.note or None)
            people.append(helper)
        return people

    if user.pk == report.reporter_account_id:
        return [claimer]
    if report.offers.filter(account=user, status=OfferStatus.MATCHED).exists():
        return [claimer]
    return None


def set_consent(row, share):
    """Record or withdraw one person's consent on one row (a report, a case or an offer)."""
    row.contact_share_consent = share
    row.contact_share_consent_at = timezone.now() if share else None
    row.save(update_fields=["contact_share_consent", "contact_share_consent_at"])
    return {"contact_shared": share}
