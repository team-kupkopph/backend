"""Review steps 16, 17, 18, 19 and 27 (MO13, MO14, MO16, MO22, MO25, MO29) · the small things that were true only by luck.

16  only a report that was CREATED spends the daily allowance; your own thing is refused, and spends nothing
17  the target as it stood when it was reported is kept, and the moderator is told if it has changed since
18  the lock and the "already decided" guard live in resolve_flag, so no caller can overwrite a decision
19  the first look is kept whoever decides later
27  the database knows what a flag may point at and serves the queue from one index"""
import pytest
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction

from accounts.factories import AccountFactory
from community.models import StoryPost
from listings.tests.adoption_helpers import c, listing, person
from moderation import targets
from moderation.actions import AlreadyDecided, resolve_flag
from moderation.models import FlagStatus, ModerationFlag
from sagip.models import StrayReport

FLAGS = "/api/v1/moderation/flags"


def _report(who, **body):
    return c(who).post(FLAGS, {"reason_code": "spam_scam", **body}, format="json")


@pytest.fixture(autouse=True)
def _fresh_throttle_cache():
    cache.clear()
    yield
    cache.clear()


def _rates(n):
    """Set the daily allowance for this test. DRF reads its rates from a class-level dict at import, so overriding the Django
    setting does nothing: patch the dict itself (and put it back)."""
    from contextlib import contextmanager

    from rest_framework.throttling import SimpleRateThrottle

    @contextmanager
    def patched():
        rates = SimpleRateThrottle.THROTTLE_RATES
        before = rates["moderation_flag_create"]
        rates["moderation_flag_create"] = f"{n}/day"
        try:
            yield
        finally:
            rates["moderation_flag_create"] = before
    return patched()


# ---- step 16 ------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_only_a_created_report_spends_the_allowance():
    """Three calls about the same listing are one report; a refused call is none. With a limit of 2 the person can still
    file a second DIFFERENT report afterwards."""
    with _rates(2):
        who, a, b, other = person(), listing(person()), listing(person()), listing(person())
        assert _report(who, target_type="listing", target_id=str(a.pk)).status_code == 201
        for _ in range(3):                                                    # folded into the first: spends nothing
            assert _report(who, target_type="listing", target_id=str(a.pk), reason="more").status_code == 200
        assert _report(who, target_type="listing", target_id="00000000-0000-0000-0000-000000000000").status_code == 404
        assert _report(who, target_type="listing", target_id=str(b.pk)).status_code == 201     # the second allowed report
        assert _report(who, target_type="listing", target_id=str(other.pk)).status_code == 429  # the third is over the limit


@pytest.mark.django_db
def test_a_person_over_the_limit_is_told_how_long_to_wait():
    with _rates(1):
        who = person()
        assert _report(who, target_type="listing", target_id=str(listing(person()).pk)).status_code == 201
        res = _report(who, target_type="listing", target_id=str(listing(person()).pk))
    assert res.status_code == 429 and res.json()["error"]["details"]["retry_after"] > 0


@pytest.mark.django_db
def test_a_folded_report_does_not_count_even_when_it_is_the_last_one_allowed():
    with _rates(1):
        who, the_listing = person(), listing(person())
        assert _report(who, target_type="listing", target_id=str(the_listing.pk)).status_code == 201
        assert _report(who, target_type="listing", target_id=str(the_listing.pk), reason="again").status_code == 200


@pytest.mark.django_db
def test_your_own_content_is_refused_and_spends_nothing():
    with _rates(1):
        me = person()
        mine_listing = listing(me)
        mine_story = StoryPost.objects.create(author_account=me, story_type="general", caption="mine")
        mine_report = StrayReport.objects.create(species="dog", condition="injured", geom=Point(121.05, 14.63, srid=4326), reporter_account=me)
        for kind, pk in (("listing", mine_listing.pk), ("story", mine_story.pk), ("report", mine_report.pk)):
            res = _report(me, target_type=kind, target_id=str(pk))
            assert res.status_code == 422 and res.json()["error"]["code"] == "own_content", kind
        assert ModerationFlag.objects.count() == 0
        assert _report(me, target_type="listing", target_id=str(listing(person()).pk)).status_code == 201   # nothing was spent


@pytest.mark.django_db
def test_somebody_elses_content_is_still_reportable():
    assert _report(person(), target_type="story",
                   target_id=str(StoryPost.objects.create(author_account=person(), story_type="general", caption="x").pk)
                   ).status_code == 201


# ---- step 17 ------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_target_as_reported_is_kept_and_an_edit_is_noticed():
    poster = person()
    live = listing(poster, name="Bantay", story="Free to a good home")
    _report(person(), target_type="listing", target_id=str(live.pk))
    flag = ModerationFlag.objects.get()
    assert flag.target_snapshot == {"title": "Bantay", "body": "Free to a good home", "photos": []}
    now = targets.describe_target("listing", live.pk)
    assert targets.edited_since(flag.target_snapshot, now) is False

    live.story = "Send a deposit first"
    live.save()
    now = targets.describe_target("listing", live.pk)
    assert targets.edited_since(flag.target_snapshot, now) is True
    assert flag.target_snapshot["body"] == "Free to a good home"       # the evidence did not move with the edit


@pytest.mark.django_db
def test_a_repeat_report_keeps_the_first_snapshot():
    live = listing(person(), name="Bantay", story="original")
    who = person()
    _report(who, target_type="listing", target_id=str(live.pk))
    live.story = "changed"
    live.save()
    _report(who, target_type="listing", target_id=str(live.pk), reason="more")
    assert ModerationFlag.objects.get().target_snapshot["body"] == "original"


@pytest.mark.django_db
def test_nothing_to_snapshot_is_not_an_error():
    assert targets.snapshot_target("listing", AccountFactory().pk) is None           # no such listing
    assert targets.snapshot_target("message", AccountFactory().pk) is None           # a type with no surface
    assert targets.edited_since(None, {"state": "present", "title": "x"}) is False   # an older flag: cannot say
    assert targets.edited_since({"title": "a", "body": "", "photos": []}, {"state": "gone"}) is False


@pytest.mark.django_db
def test_photos_changing_counts_as_an_edit():
    assert targets.edited_since({"title": "a", "body": "b", "photos": ["u1"]},
                                {"state": "present", "title": "a", "body": "b", "photos": ["u1", "u2"]}) is True


# ---- step 18 ------------------------------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("final", [FlagStatus.ACTIONED, FlagStatus.DISMISSED])
def test_a_decided_flag_cannot_be_decided_again_by_any_caller(final):
    """The guard used to be in one view; a second caller overwrote a terminal decision and the first reviewer's stamp."""
    first, second = AccountFactory(), AccountFactory()
    flag = ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing",
                                         target_id=listing(person()).pk, reason="x")
    resolve_flag(flag, first, final)
    with pytest.raises(AlreadyDecided) as err:
        resolve_flag(flag, second, FlagStatus.DISMISSED)
    assert err.value.status == final
    flag.refresh_from_db()
    assert flag.status == final and flag.reviewed_by_id == first.pk


@pytest.mark.django_db
def test_a_stale_copy_of_the_flag_is_not_trusted():
    """The caller's object says 'open' but the row has moved on: the lock re-reads it."""
    flag = ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing",
                                         target_id=listing(person()).pk, reason="x")
    ModerationFlag.objects.filter(pk=flag.pk).update(status=FlagStatus.DISMISSED)
    assert flag.status == FlagStatus.OPEN
    with pytest.raises(AlreadyDecided):
        resolve_flag(flag, AccountFactory(), FlagStatus.ACTIONED)


@pytest.mark.django_db
def test_reviewed_can_still_be_decided():
    flag = ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing",
                                         target_id=listing(person()).pk, reason="x", status=FlagStatus.REVIEWED)
    assert resolve_flag(flag, AccountFactory(), FlagStatus.DISMISSED)[0].status == FlagStatus.DISMISSED


# ---- step 19 ------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_first_look_is_kept_when_a_later_decision_replaces_reviewed_by():
    first, second = AccountFactory(), AccountFactory()
    flag = ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing",
                                         target_id=listing(person()).pk, reason="x")
    resolve_flag(flag, first, FlagStatus.REVIEWED)
    stamped = ModerationFlag.objects.get(pk=flag.pk).first_reviewed_at
    assert stamped is not None
    resolve_flag(ModerationFlag.objects.get(pk=flag.pk), second, FlagStatus.DISMISSED)
    flag.refresh_from_db()
    assert flag.reviewed_by_id == second.pk                                   # the decider
    assert flag.first_reviewed_by_id == first.pk and flag.first_reviewed_at == stamped   # the first look


@pytest.mark.django_db
def test_closing_siblings_stamps_their_first_look_too():
    target = listing(person())
    reviewer = AccountFactory()
    main = ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing", target_id=target.pk, reason="x")
    sibling = ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing", target_id=target.pk, reason="y")
    resolve_flag(main, reviewer, FlagStatus.ACTIONED)
    sibling.refresh_from_db()
    assert sibling.first_reviewed_by_id == reviewer.pk and sibling.first_reviewed_at is not None


def test_existing_decisions_are_backfilled_as_their_first_look():
    from importlib import import_module
    m = import_module("moderation.migrations.0011_backfill_first_review")
    assert [type(op).__name__ for op in m.Migration.operations] == ["RunPython"] and m.Migration.operations[0].reverse_code is not None
    assert ("moderation", "0010_hardening") in m.Migration.dependencies


@pytest.mark.django_db
def test_the_backfill_copies_reviewed_to_first_reviewed_for_decided_flags_only():
    from importlib import import_module

    from django.apps import apps
    from django.utils import timezone
    reviewer = AccountFactory()
    decided = ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing", target_id=AccountFactory().pk,
                                            reason="x", status=FlagStatus.DISMISSED, reviewed_by=reviewer, reviewed_at=timezone.now())
    untouched = ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="listing", target_id=AccountFactory().pk,
                                              reason="y")
    import_module("moderation.migrations.0011_backfill_first_review").backfill(apps, None)
    decided.refresh_from_db()
    untouched.refresh_from_db()
    assert decided.first_reviewed_at == decided.reviewed_at and decided.first_reviewed_by_id == reviewer.pk
    assert untouched.first_reviewed_at is None


# ---- step 27 ------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_database_refuses_a_target_type_it_does_not_know():
    with pytest.raises(IntegrityError), transaction.atomic():
        ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="spaceship", target_id=AccountFactory().pk, reason="x")


def test_the_check_names_every_flag_target():
    """A new FlagTarget needs a migration anyway (choices); this makes it fail HERE if the check was not updated with it."""
    from moderation.models import FlagTarget
    check = next(k for k in ModerationFlag._meta.constraints if k.name == "moderation_flag_target_type_known")
    named = {leaf for _, leaf in check.condition.children[0:1] for leaf in leaf}
    assert named == set(FlagTarget.values)


@pytest.mark.django_db
def test_the_queue_is_served_by_one_composite_index():
    with connection.cursor() as cur:
        cur.execute("select indexname, indexdef from pg_indexes where tablename = 'moderation_flag'")
        found = dict(cur.fetchall())
    assert "idx_flag_status" not in found
    assert "(status, created_at, flag_id)" in found["idx_flag_queue"]


@pytest.mark.django_db
def test_a_reporter_with_flags_cannot_be_hard_deleted_into_system():
    """SET_NULL would turn a person into 'System'. PROTECT refuses instead."""
    from django.db.models import ProtectedError
    reporter = AccountFactory()
    ModerationFlag.objects.create(reporter_account=reporter, target_type="listing", target_id=AccountFactory().pk, reason="x")
    with pytest.raises(ProtectedError), transaction.atomic():
        reporter.delete()


@pytest.mark.django_db(transaction=True)
def test_the_check_runs_without_a_surrounding_transaction():
    """The test database wraps every test in a transaction, which hides a `select_for_update` outside one. Production has no
    ATOMIC_REQUESTS, so the pre-check must be a plain read. Real autocommit here proves it."""
    who, the_listing = person(), listing(person())
    assert _report(who, target_type="listing", target_id=str(the_listing.pk)).status_code == 201
    assert _report(who, target_type="listing", target_id=str(the_listing.pk), reason="more").status_code == 200
