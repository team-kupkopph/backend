"""US-W3 · the wishlist's read models (AW17, AW18).

The list used to carry only a need's own columns, so the app fanned out one
`GET /needs/{id}/pledges` per need to learn "Pledged N", and every list stopped at 20 without
saying so. These readers put the pledge aggregates on the row (one query, however many rows),
and page the lists. Views stay thin: they pick the queryset and call one of these.

`quantity_pledged` / `pledge_count` count only `pledged` pledges (a promise still open):
delivered ones are already in `quantity_received`, cancelled ones are nothing.
"""
from django.db.models import (
    BooleanField,
    Case,
    Count,
    Exists,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
    Window,
)
from django.db.models.functions import Coalesce, RowNumber

from accounts.models import Account, Address
from common.cities import city_variants
from shelter.models import DonationQr, QrStatus, ShelterProfile

from .models import NeedStatus, PledgeStatus, ShelterNeed
from .needs import live_needs, live_shelter_ids

PAGE_SIZE = 20
MAX_PAGE = 10**6


def page_number(request):
    """`?page=` as a positive int; anything else (missing, non-integer, 0, negative) is page 1."""
    try:
        # Clamped: a 40-digit page would overflow the OFFSET's bigint and 500 a public read.
        return min(max(1, int(request.query_params.get("page") or 1)), MAX_PAGE)
    except (TypeError, ValueError):
        return 1


def page_window(request):
    """(page, slice) for `?page=`: one row past the page, to learn whether another follows."""
    page = page_number(request)
    start = (page - 1) * PAGE_SIZE
    return page, slice(start, start + PAGE_SIZE + 1)


def trim_page(rows, page):
    """(rows, next_page) from a list fetched through `page_window`'s slice."""
    return rows[:PAGE_SIZE], (page + 1 if len(rows) > PAGE_SIZE else None)


def visible_needs(viewer):
    """Which needs `viewer` may read: a live shelter's wishlist (`live_needs()`), plus — R3 —
    the viewer's own, whatever its shelter's approval state."""
    qs = live_needs()
    if viewer is not None and viewer.is_authenticated:
        qs = qs | ShelterNeed.objects.filter(shelter_account_id=viewer.pk)
    return qs


def donation_instructions(account_id, viewer):
    """The shelter's drop-off how-to, with the same visibility as its needs: a shelter the
    public can't see doesn't hand it out either (its owner always can read it). One query."""
    qs = ShelterProfile.objects.filter(account_id=account_id)
    if not (viewer is not None and viewer.is_authenticated and str(viewer.pk) == str(account_id)):
        qs = qs.filter(account_id__in=live_shelter_ids())
    return qs.values_list("donation_instructions", flat=True).first() or ""


def shelter_card(account_id, viewer):
    """T8 · who the shelter is, for a donor: its name, description, logo and hours. Same
    visibility as its needs and drop-off note (a live shelter, or its owner), and NEVER the contact
    person, official email or phone, or the vet — those are for the shelter's own eyes."""
    qs = ShelterProfile.objects.filter(account_id=account_id)
    if not (viewer is not None and viewer.is_authenticated and str(viewer.pk) == str(account_id)):
        qs = qs.filter(account_id__in=live_shelter_ids())
    p = qs.first()
    if p is None:
        return None
    return {"org_name": p.org_name, "description": p.description or None,
            "logo_url": p.logo_url or None, "operating_hours": p.operating_hours or None}


def need_rows(qs, viewer, window=None):
    """Serialise `qs` (an ordered ShelterNeed queryset, not yet sliced) with its pledge
    aggregates in ONE query. `window` is an optional `slice` applied after the annotation.

    Keys: the need's own columns (every old key keeps its meaning) plus `unit`,
    `quantity_pledged`, `pledge_count`, `created_at`, `fulfilled_at`. The owning shelter's rows
    also carry `awaiting_confirm`; any signed-in viewer's carry `my_pledged`. A guest gets
    neither key."""
    signed_in = viewer is not None and viewer.is_authenticated
    pledged = Q(pledges__status=PledgeStatus.PLEDGED)
    qs = qs.annotate(
        _qty_pledged=Coalesce(Sum("pledges__quantity", filter=pledged), 0),
        _pledge_count=Count("pledges", filter=pledged),
    )
    if signed_in:
        qs = qs.annotate(_my_pledged=Coalesce(
            Sum("pledges__quantity", filter=pledged & Q(pledges__pledger_account_id=viewer.pk)), 0))
    if window is not None:
        qs = qs[window]
    rows = []
    for n in qs:
        row = {"need_id": str(n.pk), "title": n.title, "category": n.category,
               "description": n.description, "quantity_needed": n.quantity_needed,
               "quantity_received": n.quantity_received, "status": n.status,
               "unit": n.unit, "quantity_pledged": n._qty_pledged,
               "pledge_count": n._pledge_count,
               "created_at": n.created_at.isoformat(),
               "fulfilled_at": n.fulfilled_at.isoformat() if n.fulfilled_at else None}
        if signed_in and viewer.pk == n.shelter_account_id:
            # B1 · "to confirm" counts what the shelter can actually confirm: receipt on a CLOSED
            # need is refused (409 need_not_open), so a pre-refactor orphan pledge there is not an
            # action. A `fulfilled` need's leftovers are still confirmable (R1). The dashboard
            # (`shelter/views.py`) applies the same rule, so the row tags and its count agree.
            row["awaiting_confirm"] = 0 if n.status == NeedStatus.CLOSED else n._pledge_count
        if signed_in:
            row["my_pledged"] = n._my_pledged
        rows.append(row)
    return rows


def pledge_rows(qs):
    """The owning shelter's view of a need's pledges. This is the ONLY read that carries a
    pledge's free-text `note` (R2): it is for the shelter's eyes, never the public's."""
    return [{"pledge_id": str(p.pk), "quantity": p.quantity, "status": p.status,
             "pledger_name": p.pledger_account.display_name,
             "created_at": p.created_at.isoformat(), "note": p.note,
             "quantity_received": p.quantity_received,
             "decided_at": p.decided_at.isoformat() if p.decided_at else None,
             "cancel_reason": p.cancel_reason} for p in qs]


def my_pledge_rows(qs):
    """The donor's own pledges (My Donations). No `note`, and not filtered by whether the
    shelter is still live: a donor keeps seeing what they pledged."""
    return [{"pledge_id": str(p.pk), "quantity": p.quantity, "status": p.status,
             "created_at": p.created_at.isoformat(),
             "decided_at": p.decided_at.isoformat() if p.decided_at else None,
             "quantity_received": p.quantity_received, "cancel_reason": p.cancel_reason,
             "need": {"need_id": str(p.need_id), "title": p.need.title,
                      "category": p.need.category, "status": p.need.status,
                      "unit": p.need.unit,
                      "shelter_account_id": str(p.need.shelter_account_id),
                      "shelter_name": donor_facing_shelter_name(p.need.shelter_account)}}
            for p in qs]


def donor_facing_shelter_name(shelter):
    """The account's name — except once a deleted shelter has been purged, when that name is
    "Deleted user" and the organisation's (which the purge keeps, PR-6) says who the donor gave to.
    A live shelter reads exactly as before."""
    if shelter.anonymized_at is not None:
        try:
            org_name = shelter.shelter_profile.org_name
        except ShelterProfile.DoesNotExist:
            org_name = ""
        if org_name:
            return org_name
    return shelter.display_name


def ways_to_help_rows(viewer_city, window):
    """US-W4 · a page of the shelters someone can help: live (AW2/AW31) and with an open need or a live donation QR.

    Near the viewer first — the shelter's primary-address city against `city_variants(viewer_city)`, the Adopt feed's
    matching; a shelter with no address is never near but is still listed (that is the EN-5 shelter this exists for).
    Then the most recent open need, QR-only shelters last, then name and id so pages are stable. Every column is a
    correlated subquery (no GROUP BY), and the two newest titles per shelter come from one windowed query: two queries
    a page, however many shelters.
    """
    open_needs = ShelterNeed.objects.filter(shelter_account=OuterRef("pk"), status=NeedStatus.OPEN)
    variants = city_variants(viewer_city)
    near = Q()
    for variant in variants:
        near |= Q(home_city__iexact=variant)
    qs = (Account.objects.filter(pk__in=live_shelter_ids())
          .annotate(
              open_count=Coalesce(Subquery(open_needs.order_by().values("shelter_account")
                                           .annotate(n=Count("pk")).values("n")[:1]), 0),
              latest_need=Subquery(open_needs.order_by("-created_at").values("created_at")[:1]),
              takes_qr=Exists(DonationQr.objects.filter(account=OuterRef("pk"), status=QrStatus.LIVE)),
              # A shelter should have one primary address; if it has several, the first by id, so the answer is stable.
              home_city=Coalesce(Subquery(Address.objects.filter(account=OuterRef("pk"), is_primary=True)
                                          .order_by("address_id").values("city")[:1]), Value("")),
              org=Coalesce(Subquery(ShelterProfile.objects.filter(account=OuterRef("pk")).values("org_name")[:1]),
                           F("display_name")))
          .annotate(near=Case(When(near, then=Value(True)), default=Value(False), output_field=BooleanField())
                    if variants else Value(False, output_field=BooleanField()))
          .filter(Q(open_count__gt=0) | Q(takes_qr=True))
          .order_by("-near", F("latest_need").desc(nulls_last=True), "org", "pk"))
    shelters = list(qs[window])
    titles = {}
    for account_id, title in (ShelterNeed.objects
                              .filter(shelter_account_id__in=[s.pk for s in shelters], status=NeedStatus.OPEN)
                              .annotate(rank=Window(RowNumber(), partition_by=F("shelter_account_id"),
                                                    order_by=[F("created_at").desc(), F("need_id")]))
                              .filter(rank__lte=2).order_by("shelter_account_id", "rank")
                              .values_list("shelter_account_id", "title")):
        titles.setdefault(account_id, []).append(title)
    return [{"account_id": str(s.pk), "org_name": s.org, "city": s.home_city, "near": s.near,
             "open_needs": s.open_count, "need_titles": titles.get(s.pk, []), "takes_qr": s.takes_qr}
            for s in shelters]
