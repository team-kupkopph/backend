"""Task 5 · wishlist reads (AW17, AW18, US-W3).

The list carries the pledge aggregates (so the app stops fanning out one `GET /needs/{id}/pledges`
per need), is paged instead of silently truncating at 20, a single need has a detail read, and
`/me/pledges` pages too. Every old key keeps its meaning; the new ones are additive.
"""
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.factories import AccountFactory
from community.models import NeedPledge, NeedStatus, PledgeStatus, ShelterNeed
from community.tests.test_needs import _hdr, _need, _pledge, _shelter
from shelter.models import ShelterProfile

OLD_KEYS = {"need_id", "title", "category", "description", "quantity_needed",
            "quantity_received", "status"}
NEW_KEYS = {"unit", "quantity_pledged", "pledge_count", "created_at", "fulfilled_at"}


def _list(client, shelter, query="", who=None):
    url = f"/api/v1/shelters/{shelter.pk}/needs{query}"
    return client.get(url, **(_hdr(who) if who else {}))


def _many_needs(shelter, n, pledges_each=3):
    for i in range(n):
        need = ShelterNeed.objects.create(shelter_account=shelter, title=f"Need {i}",
                                          category="food", quantity_needed=50)
        for _ in range(pledges_each):
            _pledge(need, quantity=2)


@pytest.mark.django_db
def test_list_shape_is_a_superset(client):
    sh = _shelter()
    need = ShelterNeed.objects.create(shelter_account=sh, title="Dog food", category="food",
                                      description="Dry kibble", quantity_needed=8,
                                      quantity_received=3, unit="kg")
    res = _list(client, sh)
    assert res.status_code == 200
    body = res.json()
    # T8 added `shelter` (who the shelter is: name, description, logo, hours). Additive: every old key stays.
    assert set(body) == {"results", "next", "donation_instructions", "shelter"}
    assert body["next"] is None and body["donation_instructions"] == ""
    row = body["results"][0]
    assert OLD_KEYS | NEW_KEYS <= set(row)
    # the old keys keep their meaning
    assert row["need_id"] == str(need.pk) and row["title"] == "Dog food"
    assert row["category"] == "food" and row["description"] == "Dry kibble"
    assert row["quantity_needed"] == 8 and row["quantity_received"] == 3
    assert row["status"] == "open"
    # the new ones
    assert row["unit"] == "kg" and row["quantity_pledged"] == 0 and row["pledge_count"] == 0
    assert row["created_at"] == need.created_at.isoformat() and row["fulfilled_at"] is None
    # a guest sees neither owner-only nor viewer-only keys
    assert "awaiting_confirm" not in row and "my_pledged" not in row


@pytest.mark.django_db
def test_list_aggregates_count_only_pledged(client):
    sh = _shelter()
    need = _need(sh, needed=50)
    for q in (2, 2, 1):
        _pledge(need, quantity=q)
    _pledge(need, quantity=4, status=PledgeStatus.DELIVERED)
    _pledge(need, quantity=9, status=PledgeStatus.CANCELLED)
    row = _list(client, sh).json()["results"][0]
    assert row["quantity_pledged"] == 5 and row["pledge_count"] == 3


@pytest.mark.django_db
def test_a_need_with_no_pledges_reports_zero_not_null(client):
    sh = _shelter()
    _need(sh)
    row = _list(client, sh, who=AccountFactory()).json()["results"][0]
    assert row["quantity_pledged"] == 0 and row["pledge_count"] == 0 and row["my_pledged"] == 0


@pytest.mark.django_db
def test_fulfilled_at_is_isoformat_when_set(client):
    sh = _shelter()
    need = _need(sh, needed=2, received=2, status=NeedStatus.FULFILLED)
    stamp = timezone.now() - timedelta(days=1)
    ShelterNeed.objects.filter(pk=need.pk).update(fulfilled_at=stamp)
    row = _list(client, sh).json()["results"][0]
    assert row["fulfilled_at"] == stamp.isoformat()


@pytest.mark.django_db
def test_list_is_one_query(client, django_assert_max_num_queries):
    """The read costs the same however many needs it returns: the needs and their aggregates
    in ONE query, the shelter's instructions in one more, plus the token's account lookup."""
    sh = _shelter()
    _many_needs(sh, 25)
    viewer = AccountFactory()
    with django_assert_max_num_queries(4):
        res = _list(client, sh, who=viewer)
    assert len(res.json()["results"]) == 20 and res.json()["next"] == 2


@pytest.mark.django_db
def test_query_count_does_not_scale_with_the_number_of_needs(client):
    viewer = AccountFactory()

    def count(n):
        sh = _shelter()
        _many_needs(sh, n)
        with CaptureQueriesContext(connection) as ctx:
            res = _list(client, sh, who=viewer)
        assert len(res.json()["results"]) == min(n, 20)
        return len(ctx)

    assert count(5) == count(25)


@pytest.mark.django_db
def test_list_pages_at_20_with_next(client):
    sh = _shelter()
    base = timezone.now() - timedelta(hours=1)
    for i in range(21):
        need = ShelterNeed.objects.create(shelter_account=sh, title=f"Need {i}", category="food")
        ShelterNeed.objects.filter(pk=need.pk).update(created_at=base + timedelta(minutes=i))
    first = _list(client, sh).json()
    assert len(first["results"]) == 20 and first["next"] == 2
    assert first["results"][0]["title"] == "Need 20"      # newest first
    second = _list(client, sh, "?page=2").json()
    assert [n["title"] for n in second["results"]] == ["Need 0"] and second["next"] is None
    assert len({n["need_id"] for n in first["results"] + second["results"]}) == 21


@pytest.mark.django_db
def test_page_beyond_the_last_is_empty_not_404(client):
    sh = _shelter()
    _need(sh)
    res = _list(client, sh, "?page=9")
    assert res.status_code == 200
    assert res.json()["results"] == [] and res.json()["next"] is None


@pytest.mark.django_db
@pytest.mark.parametrize("page", ["abc", "0", "-3", ""])
def test_a_bad_page_is_page_one(client, page):
    sh = _shelter()
    need = _need(sh)
    res = _list(client, sh, f"?page={page}")
    assert res.status_code == 200
    assert [n["need_id"] for n in res.json()["results"]] == [str(need.pk)]


@pytest.mark.django_db
def test_equal_timestamps_page_without_duplicates_or_gaps(client):
    sh = _shelter()
    stamp = timezone.now() - timedelta(hours=1)
    for i in range(25):
        need = ShelterNeed.objects.create(shelter_account=sh, title=f"Need {i}", category="food")
        ShelterNeed.objects.filter(pk=need.pk).update(created_at=stamp)
    ids = [n["need_id"] for n in _list(client, sh).json()["results"]]
    ids += [n["need_id"] for n in _list(client, sh, "?page=2").json()["results"]]
    assert len(ids) == 25 and len(set(ids)) == 25


@pytest.mark.django_db
def test_status_filter_still_applies_with_paging(client):
    sh = _shelter()
    open_need = _need(sh)
    _need(sh, status=NeedStatus.CLOSED)
    res = _list(client, sh, "?status=open").json()
    assert [n["need_id"] for n in res["results"]] == [str(open_need.pk)]


@pytest.mark.django_db
def test_list_carries_the_shelters_donation_instructions(client):
    sh = _shelter()
    ShelterProfile.objects.create(account=sh, org_name="Happy Paws", org_type="shelter",
                                  donation_instructions="Drop off at the gate, 9-5.")
    _need(sh)
    assert _list(client, sh).json()["donation_instructions"] == "Drop off at the gate, 9-5."


@pytest.mark.django_db
def test_a_hidden_shelters_instructions_do_not_leak(client):
    sh = _shelter(approved=False)
    ShelterProfile.objects.create(account=sh, org_name="Pending", org_type="shelter",
                                  donation_instructions="secret how-to")
    _need(sh)
    guest = _list(client, sh).json()
    assert guest["results"] == [] and guest["donation_instructions"] == ""
    assert _list(client, sh, who=sh).json()["donation_instructions"] == "secret how-to"


@pytest.mark.django_db
def test_awaiting_confirm_only_for_the_owner(client):
    sh = _shelter()
    need = _need(sh, needed=50)
    _pledge(need, quantity=3)
    _pledge(need, quantity=2)
    _pledge(need, quantity=9, status=PledgeStatus.DELIVERED)
    mine = _list(client, sh, who=sh).json()["results"][0]
    assert mine["awaiting_confirm"] == 2
    for viewer in (None, AccountFactory(), _shelter()):
        row = _list(client, sh, who=viewer).json()["results"][0]
        assert "awaiting_confirm" not in row


@pytest.mark.django_db
def test_awaiting_confirm_is_zero_on_a_closed_need_but_counts_a_fulfilled_one(client):
    """B1 · a pre-refactor `closed` need can still hold a `pledged` pledge (the old close never
    cascaded). Receipt on a closed need is refused (409), so it must not read as "to confirm"
    — the dashboard already excludes it. A `fulfilled` need's leftover pledges ARE confirmable
    (Ruling R1), so they still count."""
    sh = _shelter()
    closed = _need(sh, status=NeedStatus.CLOSED)
    fulfilled = _need(sh, needed=5, received=5, status=NeedStatus.FULFILLED)
    open_ = _need(sh, needed=50)
    for n in (closed, fulfilled, open_):
        _pledge(n, quantity=2)
    rows = {r["need_id"]: r for r in _list(client, sh, who=sh).json()["results"]}
    assert len(rows) == 3, "the scan must find all three needs"
    assert rows[str(closed.pk)]["awaiting_confirm"] == 0
    assert rows[str(fulfilled.pk)]["awaiting_confirm"] == 1
    assert rows[str(open_.pk)]["awaiting_confirm"] == 1
    # the public aggregates still describe the data as it is
    assert rows[str(closed.pk)]["pledge_count"] == 1
    # and the single-need read agrees with the list
    assert client.get(f"/api/v1/needs/{closed.pk}", **_hdr(sh)).json()["awaiting_confirm"] == 0


@pytest.mark.django_db
def test_my_pledged_is_the_viewers_own_and_absent_for_guests(client):
    sh = _shelter()
    need = _need(sh, needed=50)
    giver, other = AccountFactory(), AccountFactory()
    _pledge(need, pledger=giver, quantity=3)
    _pledge(need, pledger=giver, quantity=4)
    _pledge(need, pledger=giver, quantity=7, status=PledgeStatus.CANCELLED)
    _pledge(need, pledger=other, quantity=5)
    assert _list(client, sh, who=giver).json()["results"][0]["my_pledged"] == 7
    assert _list(client, sh, who=other).json()["results"][0]["my_pledged"] == 5
    assert _list(client, sh, who=AccountFactory()).json()["results"][0]["my_pledged"] == 0
    assert "my_pledged" not in _list(client, sh).json()["results"][0]
    # the shelter's own row: everyone's pledges count in the totals, none are "mine"
    owner_row = _list(client, sh, who=sh).json()["results"][0]
    assert owner_row["quantity_pledged"] == 12 and owner_row["my_pledged"] == 0


# --- GET /needs/{id} ------------------------------------------------------------

@pytest.mark.django_db
def test_need_detail_has_my_pledged(client):
    sh = _shelter(display_name="Happy Paws")
    ShelterProfile.objects.create(account=sh, org_name="Happy Paws", org_type="shelter",
                                  donation_instructions="Ring the bell.")
    need = ShelterNeed.objects.create(shelter_account=sh, title="Blankets", category="supplies",
                                      quantity_needed=10, unit="pcs")
    giver = AccountFactory()
    _pledge(need, pledger=giver, quantity=3)
    _pledge(need, quantity=2)
    res = client.get(f"/api/v1/needs/{need.pk}", **_hdr(giver))
    assert res.status_code == 200
    body = res.json()
    assert OLD_KEYS | NEW_KEYS <= set(body)
    assert body["need_id"] == str(need.pk) and body["unit"] == "pcs"
    assert body["quantity_pledged"] == 5 and body["pledge_count"] == 2
    assert body["my_pledged"] == 3 and "awaiting_confirm" not in body
    assert body["shelter_name"] == "Happy Paws"
    assert body["shelter_account_id"] == str(sh.pk)
    assert body["donation_instructions"] == "Ring the bell."


@pytest.mark.django_db
def test_need_detail_for_a_guest_has_no_viewer_keys(client):
    need = _need()
    body = client.get(f"/api/v1/needs/{need.pk}").json()
    assert "my_pledged" not in body and "awaiting_confirm" not in body
    assert body["donation_instructions"] == ""


@pytest.mark.django_db
def test_need_detail_gives_the_owner_awaiting_confirm(client):
    sh = _shelter()
    need = _need(sh)
    _pledge(need)
    body = client.get(f"/api/v1/needs/{need.pk}", **_hdr(sh)).json()
    assert body["awaiting_confirm"] == 1 and body["my_pledged"] == 0


@pytest.mark.django_db
def test_need_detail_404s_outside_live_needs_but_not_for_the_owner(client):
    sh = _shelter(approved=False)
    need = _need(sh)
    for viewer in (None, AccountFactory(), _shelter()):
        res = client.get(f"/api/v1/needs/{need.pk}", **(_hdr(viewer) if viewer else {}))
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "not_found"
        assert res.json()["error"]["message"] == "No such need."
    mine = client.get(f"/api/v1/needs/{need.pk}", **_hdr(sh))
    assert mine.status_code == 200 and mine.json()["need_id"] == str(need.pk)


@pytest.mark.django_db
def test_need_detail_404s_for_a_suspended_shelter_and_an_unknown_id(client):
    need = _need(_shelter(status="suspended"))
    assert client.get(f"/api/v1/needs/{need.pk}").status_code == 404
    assert client.get("/api/v1/needs/00000000-0000-0000-0000-000000000000").status_code == 404


@pytest.mark.django_db
def test_patch_on_the_detail_route_is_still_owner_only_and_echoes_unit(client):
    sh = _shelter()
    need = _need(sh)
    ShelterNeed.objects.filter(pk=need.pk).update(unit="kg")
    body = {"title": "Dog food (large)"}
    assert client.patch(f"/api/v1/needs/{need.pk}", body,
                        content_type="application/json").status_code == 401
    assert client.patch(f"/api/v1/needs/{need.pk}", body, content_type="application/json",
                        **_hdr(AccountFactory())).status_code == 403
    res = client.patch(f"/api/v1/needs/{need.pk}", body, content_type="application/json",
                       **_hdr(sh))
    assert res.status_code == 200
    assert res.json()["title"] == "Dog food (large)" and res.json()["unit"] == "kg"
    assert OLD_KEYS <= set(res.json())


# --- GET /needs/{id}/pledges ----------------------------------------------------

@pytest.mark.django_db
def test_owner_pledge_rows_carry_the_decision_fields_and_note(client):
    sh = _shelter()
    need = _need(sh, needed=20)
    giver = AccountFactory(display_name="Rina")
    p = NeedPledge.objects.create(need=need, pledger_account=giver, quantity=3,
                                  note="Saturday morning")
    done = NeedPledge.objects.create(need=need, pledger_account=AccountFactory(), quantity=2,
                                     status=PledgeStatus.DELIVERED, quantity_received=4,
                                     decided_at=timezone.now())
    gone = NeedPledge.objects.create(need=need, pledger_account=AccountFactory(), quantity=2,
                                     status=PledgeStatus.CANCELLED, cancel_reason="donor",
                                     decided_at=timezone.now())
    rows = {r["pledge_id"]: r for r in
            client.get(f"/api/v1/needs/{need.pk}/pledges", **_hdr(sh)).json()["results"]}
    assert {"pledge_id", "quantity", "status", "pledger_name", "created_at", "note",
            "quantity_received", "decided_at", "cancel_reason"} <= set(rows[str(p.pk)])
    assert rows[str(p.pk)]["note"] == "Saturday morning"
    assert rows[str(p.pk)]["pledger_name"] == "Rina" and rows[str(p.pk)]["quantity"] == 3
    assert rows[str(p.pk)]["quantity_received"] is None and rows[str(p.pk)]["decided_at"] is None
    assert rows[str(p.pk)]["cancel_reason"] is None
    assert rows[str(done.pk)]["quantity_received"] == 4
    assert rows[str(done.pk)]["decided_at"] == done.decided_at.isoformat()
    assert rows[str(gone.pk)]["cancel_reason"] == "donor"


@pytest.mark.django_db
def test_the_pledge_note_is_in_no_other_read(client):
    sh = _shelter()
    need = _need(sh)
    giver = AccountFactory()
    NeedPledge.objects.create(need=need, pledger_account=giver, quantity=1, note="PRIVATE-NOTE")
    for res in (client.get(f"/api/v1/shelters/{sh.pk}/needs", **_hdr(giver)),
                client.get(f"/api/v1/shelters/{sh.pk}/needs", **_hdr(sh)),
                client.get(f"/api/v1/needs/{need.pk}", **_hdr(giver)),
                client.get(f"/api/v1/needs/{need.pk}", **_hdr(sh)),
                client.get("/api/v1/me/pledges", **_hdr(giver))):
        assert res.status_code == 200
        assert "PRIVATE-NOTE" not in res.content.decode()
        assert '"note"' not in res.content.decode()


# --- GET /me/pledges ------------------------------------------------------------

@pytest.mark.django_db
def test_my_pledges_pages_and_carries_need_status(client):
    sh = _shelter(display_name="Happy Paws")
    giver = AccountFactory()
    base = timezone.now() - timedelta(hours=1)
    for i in range(21):
        need = ShelterNeed.objects.create(shelter_account=sh, title=f"Need {i}",
                                          category="food", unit="kg")
        p = _pledge(need, pledger=giver, quantity=i + 1)
        NeedPledge.objects.filter(pk=p.pk).update(created_at=base + timedelta(minutes=i))
    first = client.get("/api/v1/me/pledges", **_hdr(giver)).json()
    assert set(first) == {"results", "next"}
    assert len(first["results"]) == 20 and first["next"] == 2
    top = first["results"][0]
    assert top["quantity"] == 21 and top["need"]["title"] == "Need 20"      # newest first
    assert {"pledge_id", "quantity", "status", "created_at", "decided_at",
            "quantity_received", "cancel_reason", "need"} <= set(top)
    assert {"need_id", "title", "category", "shelter_name", "status", "unit",
            "shelter_account_id"} <= set(top["need"])
    assert top["need"]["status"] == "open" and top["need"]["unit"] == "kg"
    assert top["need"]["shelter_account_id"] == str(sh.pk)
    assert top["need"]["shelter_name"] == "Happy Paws"
    assert top["decided_at"] is None and top["quantity_received"] is None
    assert top["cancel_reason"] is None
    second = client.get("/api/v1/me/pledges?page=2", **_hdr(giver)).json()
    assert len(second["results"]) == 1 and second["next"] is None
    beyond = client.get("/api/v1/me/pledges?page=5", **_hdr(giver)).json()
    assert beyond == {"results": [], "next": None}
    assert client.get("/api/v1/me/pledges?page=junk", **_hdr(giver)).json()["next"] == 2


@pytest.mark.django_db
def test_my_pledges_still_shows_a_suspended_shelters_need(client):
    sh = _shelter(status="suspended")
    giver = AccountFactory()
    p = _pledge(_need(sh), pledger=giver)
    rows = client.get("/api/v1/me/pledges", **_hdr(giver)).json()["results"]
    assert [r["pledge_id"] for r in rows] == [str(p.pk)]


@pytest.mark.django_db
def test_my_pledges_decision_fields_show_the_outcome(client):
    giver = AccountFactory()
    need = _need(needed=20)
    when = timezone.now()
    NeedPledge.objects.create(need=need, pledger_account=giver, quantity=3,
                              status=PledgeStatus.DELIVERED, quantity_received=5, decided_at=when)
    row = client.get("/api/v1/me/pledges", **_hdr(giver)).json()["results"][0]
    assert row["status"] == "delivered" and row["quantity_received"] == 5
    assert row["decided_at"] == when.isoformat()


@pytest.mark.django_db
def test_my_pledges_is_one_query_per_page(client):
    giver = AccountFactory()
    sh = _shelter()
    for _ in range(25):
        _pledge(_need(sh), pledger=giver)
    with CaptureQueriesContext(connection) as ctx:
        res = client.get("/api/v1/me/pledges", **_hdr(giver))
    assert len(res.json()["results"]) == 20
    assert len(ctx) <= 3         # the token's account lookup + the page


@pytest.mark.django_db
@pytest.mark.parametrize("page", [str(10**18), str(10**30), "9" * 40])
def test_a_huge_page_is_an_empty_page_not_a_500(client, page):
    """The OFFSET of a 40-digit page overflows bigint; the page number is clamped instead."""
    sh = _shelter()
    _need(sh)
    giver = AccountFactory()
    _pledge(_need(sh), pledger=giver)
    listed = _list(client, sh, f"?page={page}")
    assert listed.status_code == 200
    assert listed.json()["results"] == [] and listed.json()["next"] is None
    mine = client.get(f"/api/v1/me/pledges?page={page}", **_hdr(giver))
    assert mine.status_code == 200
    assert mine.json() == {"results": [], "next": None}


# --- expand/contract · the previous release must still be able to insert ------------

NEW_TEXT_COLUMNS = [("shelter_need", "unit"), ("need_pledge", "note"),
                    ("shelter_profile", "donation_instructions")]


@pytest.mark.django_db
@pytest.mark.parametrize("table,column", NEW_TEXT_COLUMNS)
def test_new_not_null_text_columns_keep_a_database_default(table, column):
    """Migrate runs before the new code ships, and a rollback leaves the schema: the old
    release's INSERTs omit these columns, so the DB itself must default them."""
    with connection.cursor() as cur:
        cur.execute("SELECT column_default, is_nullable FROM information_schema.columns "
                    "WHERE table_name = %s AND column_name = %s", [table, column])
        default, nullable = cur.fetchone()
    assert nullable == "NO" and default is not None and "''" in default


@pytest.mark.django_db
def test_a_raw_insert_omitting_the_new_columns_succeeds():
    """What the previous release's pledge / need / profile inserts look like."""
    import uuid
    sh = _shelter()
    now = timezone.now()
    need_id, pledge_id = uuid.uuid4(), uuid.uuid4()
    with connection.cursor() as cur:
        cur.execute("INSERT INTO shelter_need (need_id, shelter_account_id, title, category, "
                    "description, quantity_needed, quantity_received, status, created_at) "
                    "VALUES (%s, %s, 'Rice', 'food', '', 5, 0, 'open', %s)",
                    [need_id, sh.pk, now])
        cur.execute("INSERT INTO need_pledge (pledge_id, need_id, pledger_account_id, quantity, "
                    "status, created_at) VALUES (%s, %s, %s, 1, 'pledged', %s)",
                    [pledge_id, need_id, AccountFactory().pk, now])
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'shelter_profile' AND is_nullable = 'NO' "
                    "AND column_default IS NULL")
        required = {r[0] for r in cur.fetchall()}
    assert ShelterNeed.objects.get(pk=need_id).unit == ""
    assert NeedPledge.objects.get(pk=pledge_id).note == ""
    # the profile table has other required columns the old release always supplied; the new
    # one must not add to that list.
    assert "donation_instructions" not in required


# --- a deleted shelter in its donors' records (PR-6) -------------------------------------------

def _anonymised(account):
    from accounts.purge import anonymize_account
    return anonymize_account(account)


@pytest.mark.django_db
def test_my_pledges_name_a_deleted_shelter_by_its_organisation(client):
    """PR-6 · the purge renames the account "Deleted user", which is what My donations showed.
    The organisation's name survives the purge, so a donor still sees who they gave to."""
    sh = _shelter(display_name="Gone Rescue")
    ShelterProfile.objects.create(account=sh, org_name="Gone Rescue Inc.", org_type="shelter")
    giver = AccountFactory()
    _pledge(_need(sh), pledger=giver, quantity=2)
    _anonymised(sh)

    row = client.get("/api/v1/me/pledges", **_hdr(giver)).json()["results"][0]
    assert row["need"]["shelter_name"] == "Gone Rescue Inc."


@pytest.mark.django_db
def test_my_pledges_keep_a_live_shelters_display_name(client):
    sh = _shelter(display_name="Paws Manila")
    ShelterProfile.objects.create(account=sh, org_name="PAWS Manila Inc.", org_type="shelter")
    giver = AccountFactory()
    _pledge(_need(sh), pledger=giver, quantity=2)

    row = client.get("/api/v1/me/pledges", **_hdr(giver)).json()["results"][0]
    assert row["need"]["shelter_name"] == "Paws Manila"


@pytest.mark.django_db
def test_a_deleted_shelter_without_a_profile_reads_deleted_user(client):
    sh = _shelter(display_name="No Profile Rescue")
    giver = AccountFactory()
    _pledge(_need(sh), pledger=giver, quantity=1)
    _anonymised(sh)

    row = client.get("/api/v1/me/pledges", **_hdr(giver)).json()["results"][0]
    assert row["need"]["shelter_name"] == "Deleted user"


@pytest.mark.django_db
def test_naming_deleted_shelters_costs_no_query_per_row(client):
    giver = AccountFactory()

    def pledge_to_a_deleted_shelter():
        sh = _shelter()
        ShelterProfile.objects.create(account=sh, org_name="Gone", org_type="shelter")
        _pledge(_need(sh), pledger=giver)
        _anonymised(sh)

    pledge_to_a_deleted_shelter()
    with CaptureQueriesContext(connection) as one:
        client.get("/api/v1/me/pledges", **_hdr(giver))
    for _ in range(4):
        pledge_to_a_deleted_shelter()
    with CaptureQueriesContext(connection) as five:
        res = client.get("/api/v1/me/pledges", **_hdr(giver))
    assert [r["need"]["shelter_name"] for r in res.json()["results"]] == ["Gone"] * 5
    assert len(five) == len(one)
