"""SH7 / SH7b / SH8 / SH8b (shelter home review, 2026-10-29) · the inbox in SQL, and a checked query.

The inbox used to load every inquiry and signup the shelter ever had into Python, filter "open" and sort
in memory, then return 20: the cost grew with all-time history, not with the page. It is now one
`UNION ALL` of three narrow projections, filtered and ordered (`-created_at, -id`) in SQL, with a
`LIMIT` one past the page to compute `next`.

The contract tests here were written first and passed against `main` before the rewrite: the row shape
is unchanged. `count` (the total for the filter, for Requests' "{n} {segment} requests" line, step 10)
is the one new key. An unknown `kind` or `status` is now a 400 instead of a silent empty list.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from listings.models import AdoptionInquiry, AdoptionListing, InquiryStatus
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup

URL = "/api/v1/shelter/requests"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter():
    return AccountFactory(account_type="shelter", email_verified_at=timezone.now())


def _listing(poster, name="Bantay"):
    return AdoptionListing.objects.create(posted_by=poster, name=name, species="dog", city="Marikina",
                                          adoption_fee="0")


def _shift(shelter, title="", starts_at=None):
    starts_at = starts_at or timezone.now() + timedelta(days=2)
    return VolunteerShift.objects.create(shelter_account=shelter, type="walking", title=title,
                                         starts_at=starts_at, ends_at=starts_at + timedelta(hours=2),
                                         capacity=50, status=ShiftStatus.OPEN)


def _inquiry(listing, adopter, status=InquiryStatus.ACTIVE, kind="public", at=None):
    inq = AdoptionInquiry.objects.create(listing=listing, adopter_account=adopter, status=status, kind=kind)
    if at:
        AdoptionInquiry.objects.filter(pk=inq.pk).update(created_at=at)
        inq.refresh_from_db()
    return inq


def _signup(shift, status=SignupStatus.REQUESTED, at=None):
    su = VolunteerSignup.objects.create(shift=shift, volunteer_account=AccountFactory(account_type="personal"),
                                        status=status)
    if at:
        VolunteerSignup.objects.filter(pk=su.pk).update(created_at=at)
        su.refresh_from_db()
    return su


def _person(name):
    return AccountFactory(account_type="personal", display_name=name)


@pytest.mark.django_db
class TestRowContract:
    """Each kind's row, key for key. Passed against `main` before the rewrite."""

    def test_adoption_row(self, client):
        shelter = _shelter()
        inq = _inquiry(_listing(shelter, "Bantay"), _person("Ana Cruz"))
        row = client.get(URL, {"kind": "adoption"}, **_hdr(shelter)).json()["results"][0]
        assert row == {
            "kind": "adoption", "id": str(inq.pk), "title": "Bantay", "subtitle": "Ana Cruz",
            "status": "active", "created_at": inq.created_at.isoformat(),
            "target": {"route": "inquiry", "id": str(inq.pk)},
        }

    def test_volunteer_row(self, client):
        shelter = _shelter()
        starts = timezone.make_aware(timezone.datetime(2030, 10, 5, 9, 0))
        su = _signup(_shift(shelter, starts_at=starts))
        titled = _signup(_shift(shelter, title="Bath day", starts_at=starts + timedelta(days=1)))
        rows = {r["id"]: r for r in client.get(URL, {"kind": "volunteer"}, **_hdr(shelter)).json()["results"]}
        assert rows[str(su.pk)] == {
            "kind": "volunteer", "id": str(su.pk), "title": su.volunteer_account.display_name,
            "subtitle": "Sat, Oct 5 · Dog walking", "status": "requested",
            "created_at": su.created_at.isoformat(),
            "target": {"route": "shelterVolunteerRequests", "id": str(su.shift_id)},
        }
        assert rows[str(titled.pk)]["subtitle"] == "Sun, Oct 6 · Bath day"

    def test_placement_row(self, client):
        shelter = _shelter()
        worker = _person("Case Worker Lim")
        inq = _inquiry(_listing(worker, "Kalabaw"), shelter, kind="placement")
        row = client.get(URL, {"kind": "placement"}, **_hdr(shelter)).json()["results"][0]
        assert row == {
            "kind": "placement", "id": str(inq.pk), "title": "Kalabaw",
            "subtitle": "Placed by Case Worker Lim", "status": "active",
            "created_at": inq.created_at.isoformat(),
            "target": {"route": "inquiry", "id": str(inq.pk)},
        }

    def test_a_placement_is_not_an_adoption_row_for_the_poster(self, client):
        # A case worker who is ALSO a shelter: their placed inquiry is not an adoption request to them.
        poster = _shelter()
        _inquiry(_listing(poster), _shelter(), kind="placement")
        assert client.get(URL, {"kind": "adoption"}, **_hdr(poster)).json()["results"] == []


@pytest.mark.django_db
class TestOpenAndOrder:
    def test_status_open_keeps_active_placements_and_drops_closed_ones(self, client):
        shelter = _shelter()
        worker = _person("Lim")
        live = _inquiry(_listing(worker, "A"), shelter, kind="placement")
        _inquiry(_listing(worker, "B"), shelter, kind="placement", status=InquiryStatus.ADOPTED)
        _inquiry(_listing(worker, "C"), shelter, kind="placement", status=InquiryStatus.DECLINED)
        rows = client.get(URL, {"kind": "placement", "status": "open"}, **_hdr(shelter)).json()["results"]
        assert [r["id"] for r in rows] == [str(live.pk)]

    def test_status_open_drops_every_closed_adoption_and_volunteer_status(self, client):
        shelter = _shelter()
        listing = _listing(shelter)
        for st in (InquiryStatus.ADOPTED, InquiryStatus.DECLINED, InquiryStatus.WITHDRAWN):
            _inquiry(listing, _person("x"), status=st)
        shift = _shift(shelter)
        for st in (SignupStatus.APPROVED, SignupStatus.DECLINED, SignupStatus.CANCELLED):
            _signup(shift, status=st)
        body = client.get(URL, {"status": "open"}, **_hdr(shelter)).json()
        assert body["results"] == []
        assert body["count"] == 0
        assert client.get(URL, **_hdr(shelter)).json()["count"] == 6

    def test_newest_first_across_kinds_and_ties_break_by_id_descending(self, client):
        shelter = _shelter()
        t = timezone.now() - timedelta(hours=1)
        listing = _listing(shelter)
        shift = _shift(shelter)
        tied = [_inquiry(listing, _person("a"), at=t), _signup(shift, at=t),
                _inquiry(listing, _person("b"), at=t), _signup(shift, at=t)]
        newest = _signup(shift, at=t + timedelta(minutes=1))
        ids = [r["id"] for r in client.get(URL, **_hdr(shelter)).json()["results"]]
        assert ids[0] == str(newest.pk)
        assert ids[1:] == sorted((str(x.pk) for x in tied), reverse=True)


@pytest.mark.django_db
class TestPaging:
    def test_25_rows_page_1_has_20_and_next_2_page_2_has_5_and_no_next(self, client):
        shelter = _shelter()
        listing = _listing(shelter)
        base = timezone.now() - timedelta(days=1)
        made = [_inquiry(listing, _person(f"p{i}"), at=base + timedelta(minutes=i)) for i in range(25)]
        p1 = client.get(URL, {"kind": "adoption", "status": "open"}, **_hdr(shelter)).json()
        p2 = client.get(URL, {"kind": "adoption", "status": "open", "page": 2}, **_hdr(shelter)).json()
        assert (len(p1["results"]), p1["next"], p1["count"]) == (20, 2, 25)
        assert (len(p2["results"]), p2["next"], p2["count"]) == (5, None, 25)
        assert [r["id"] for r in p1["results"] + p2["results"]] == [str(m.pk) for m in reversed(made)]

    def test_exactly_20_rows_has_no_next(self, client):
        shelter = _shelter()
        listing = _listing(shelter)
        for i in range(20):
            _inquiry(listing, _person(f"p{i}"))
        assert client.get(URL, **_hdr(shelter)).json()["next"] is None

    def test_a_page_past_the_end_is_empty_not_an_error(self, client):
        shelter = _shelter()
        _inquiry(_listing(shelter), _person("a"))
        body = client.get(URL, {"page": 9}, **_hdr(shelter)).json()
        assert (body["results"], body["next"], body["count"]) == ([], None, 1)

    @pytest.mark.parametrize("page", ["0", "-3", "abc", ""])
    def test_a_junk_page_reads_as_page_1(self, client, page):
        shelter = _shelter()
        _inquiry(_listing(shelter), _person("a"))
        assert len(client.get(URL, {"page": page}, **_hdr(shelter)).json()["results"]) == 1


@pytest.mark.django_db
class TestQueryShape:
    def test_history_does_not_grow_the_query_count_or_the_rows_loaded(
            self, client, django_assert_max_num_queries):
        # 500 closed rows and 30 open ones: the page reads the page, not the history.
        shelter = _shelter()
        listing = _listing(shelter)
        adopters = [_person(f"closed{i}") for i in range(10)]   # one inquiry per (listing, adopter)
        closed_on = [_listing(shelter, f"L{i}") for i in range(50)]
        AdoptionInquiry.objects.bulk_create(
            [AdoptionInquiry(listing=lst, adopter_account=a, status=InquiryStatus.DECLINED)
             for lst in closed_on for a in adopters])
        shift = _shift(shelter)
        for _ in range(15):
            _signup(shift)
        for i in range(15):
            _inquiry(listing, _person(f"o{i}"))
        hdr = _hdr(shelter)
        # auth (1) + count (1) + the UNION page (1). Was 3 full-history scans plus auth.
        with django_assert_max_num_queries(3) as ctx:
            body = client.get(URL, {"status": "open"}, **hdr).json()
        assert (len(body["results"]), body["count"], body["next"]) == (20, 30, 2)
        union = [q["sql"] for q in ctx.captured_queries if "UNION ALL" in q["sql"]]
        assert len(union) == 2                                   # the page and its count
        assert sum("LIMIT 21" in sql for sql in union) == 1


@pytest.mark.django_db
class TestValidation:
    @pytest.mark.parametrize("params,field", [
        ({"kind": "adoptions"}, "kind"),
        ({"kind": ""}, "kind"),
        ({"status": "closed"}, "status"),
        ({"status": "OPEN"}, "status"),
    ])
    def test_an_unknown_kind_or_status_is_a_400_naming_the_field(self, client, params, field):
        shelter = _shelter()
        r = client.get(URL, params, **_hdr(shelter))
        assert r.status_code == 400, r.content
        err = r.json()["error"]
        assert (err["code"], err["field"]) == ("invalid", field)

    @pytest.mark.parametrize("params", [{}, {"kind": "adoption"}, {"kind": "volunteer"},
                                        {"kind": "placement", "status": "open"}, {"status": "open"}])
    def test_every_known_combination_is_a_200(self, client, params):
        assert client.get(URL, params, **_hdr(_shelter())).status_code == 200
