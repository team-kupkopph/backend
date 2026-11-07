"""Review step 10 (MO26, V10) · a queue a moderator can work: page on, filter by kind and category, search, and see what was flagged and
how many reports there are, without a query per row."""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.factories import AccountFactory
from adminapi.tests.conftest import full_signin
from listings.tests.adoption_helpers import c, listing, person
from moderation.models import FlagStatus, ModerationFlag

QUEUE = "/admin-api/flags"


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def _report(reporter, the_listing, code="spam_scam", note=""):
    return c(reporter).post("/api/v1/moderation/flags", {"target_type": "listing", "target_id": str(the_listing.pk),
                                                          "reason_code": code, "reason": note}, format="json")


def _rows(client, auth, query=""):
    return client.get(f"{QUEUE}{query}", **auth).json()


# ---- paging ------------------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_long_queue_pages_on_with_the_cursor_and_loses_nothing(client, auth):
    for _ in range(30):
        ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing", target_id=AccountFactory().pk, reason="x")
    first = _rows(client, auth)
    assert len(first["results"]) == 25 and first["next_cursor"]
    second = _rows(client, auth, f"?cursor={first['next_cursor']}")
    assert len(second["results"]) == 5 and second["next_cursor"] is None
    ids = [r["flag_id"] for r in first["results"] + second["results"]]
    assert len(set(ids)) == 30


# ---- what was flagged, and how many ------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_row_says_what_was_flagged_from_the_snapshot_and_how_many_reports_there_are(client, auth):
    live = listing(person(), name="Free puppies", story="Send a deposit first")
    _report(person(), live)
    _report(person(), live)
    other = listing(person(), name="Bantay")
    _report(person(), other)
    rows = _rows(client, auth)["results"]
    assert sorted((r["snippet"], r["report_count"]) for r in rows) == [("Bantay", 1), ("Free puppies", 2), ("Free puppies", 2)]


@pytest.mark.django_db
def test_a_decided_report_is_not_counted_as_live(client, auth):
    live = listing(person(), name="Free puppies")
    _report(person(), live)
    ModerationFlag.objects.filter(target_id=live.pk).update(status=FlagStatus.DISMISSED)
    _report(person(), live)
    open_rows = _rows(client, auth)["results"]
    assert [r["report_count"] for r in open_rows] == [1]


@pytest.mark.django_db
def test_a_long_title_is_cut_for_the_row_and_an_old_flag_with_no_snapshot_has_none(client, auth):
    _report(person(), listing(person(), name="N" * 79))
    ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing", target_id=AccountFactory().pk, reason="old")
    rows = _rows(client, auth)["results"]
    snippets = sorted(r["snippet"] for r in rows)
    assert snippets == ["", "N" * 79]


@pytest.mark.django_db
def test_the_queries_do_not_grow_with_the_rows(client, auth):
    for _ in range(3):
        _report(person(), listing(person()))
    with CaptureQueriesContext(connection) as small:
        _rows(client, auth)
    for _ in range(20):
        _report(person(), listing(person()))
    with CaptureQueriesContext(connection) as large:
        _rows(client, auth)
    assert len(large) == len(small), (len(small), len(large))


# ---- filter and search ------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_filter_by_category_and_by_kind(client, auth):
    _report(person(), listing(person()), code="animal_welfare")
    _report(person(), listing(person()), code="spam_scam")
    welfare = _rows(client, auth, "?reason_code=animal_welfare")["results"]
    assert [r["reason_code"] for r in welfare] == ["animal_welfare"]
    assert client.get(f"{QUEUE}?reason_code=nonsense", **auth).status_code == 400
    assert _rows(client, auth, "?target_type=story")["results"] == []
    assert len(_rows(client, auth, "?target_type=listing")["results"]) == 2


@pytest.mark.django_db
def test_search_finds_the_reporters_words_the_title_and_the_reporters_name(client, auth):
    reporter = AccountFactory(display_name="Marites Santos")
    _report(reporter, listing(person(), name="Golden pup"), note="asked for money outside the app")
    _report(person(), listing(person(), name="Quiet cat"))
    for term, expected in (("outside the app", 1), ("golden", 1), ("marites", 1), ("zzzz", 0), ("CAT", 1)):
        assert len(_rows(client, auth, f"?q={term}")["results"]) == expected, term


@pytest.mark.django_db
def test_search_and_filters_combine_with_the_status_tab(client, auth):
    live = listing(person(), name="Golden pup")
    _report(person(), live, code="spam_scam")
    ModerationFlag.objects.filter(target_id=live.pk).update(status=FlagStatus.DISMISSED)
    assert _rows(client, auth, "?q=golden")["results"] == []                                 # open tab: it was dismissed
    assert len(_rows(client, auth, "?q=golden&status=dismissed")["results"]) == 1


@pytest.mark.django_db
def test_a_blank_or_odd_search_does_not_break_the_queue(client, auth):
    _report(person(), listing(person()))
    for term in ("", "   ", "%", "_", "' OR 1=1 --", "a" * 500):
        assert client.get(f"{QUEUE}", {"q": term}, **auth).status_code == 200, term
