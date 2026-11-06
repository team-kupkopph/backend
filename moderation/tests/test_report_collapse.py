"""Review step 15 (MO12) · a second report about the same thing by the same person adds to the first instead of being thrown away.

Found on a simulator, not in review: someone typed "Asked me to pay a deposit outside the app", saw "Thanks, we will take a look", and
the server had silently kept only the earlier flag. The collapse is right (one live report per person per target: a double tap
must not write two rows); losing the words is not. And the collapse was check-then-insert with no constraint, so two taps in the
same instant could write two rows, and a `reviewed` flag escaped it altogether."""
import uuid
from importlib import import_module

import pytest
from django.db import IntegrityError, connection, transaction

from listings.tests.adoption_helpers import c, listing, person
from moderation import reports
from moderation.models import FlagStatus, ModerationFlag

URL = "/api/v1/moderation/flags"


def _flag(reporter, target_id, status=FlagStatus.OPEN, reason="first", code="harassment", target_type="listing"):
    return ModerationFlag.objects.create(reporter_account=reporter, target_type=target_type, target_id=target_id,
                                         reason=reason, reason_code=code, status=status)


def _report(reporter, target, **body):
    return c(reporter).post(URL, {"target_type": "listing", "target_id": str(target), **body}, format="json")


# ---- the words are kept -------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_second_report_adds_its_note_to_the_first_instead_of_dropping_it():
    reporter, live = person(), listing(person())
    first = _report(reporter, live.pk, reason_code="harassment", reason="rude in the inquiry")
    second = _report(reporter, live.pk, reason_code="harassment", reason="asked for a deposit outside the app")

    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["flag_id"] == second.json()["flag_id"]
    flag = ModerationFlag.objects.get()
    assert flag.reason == "rude in the inquiry\nasked for a deposit outside the app" and flag.reason_code == "harassment"


@pytest.mark.django_db
def test_a_different_category_is_tagged_in_the_note_and_the_original_category_stays():
    reporter, live = person(), listing(person())
    _report(reporter, live.pk, reason_code="harassment")
    res = _report(reporter, live.pk, reason_code="spam_scam", reason="he wants money")
    flag = ModerationFlag.objects.get()
    assert res.status_code == 200 and flag.reason_code == "harassment"
    assert flag.reason == "[Spam or a scam] he wants money"
    _report(reporter, live.pk, reason_code="misleading")              # a category alone is still information
    assert ModerationFlag.objects.get().reason == "[Spam or a scam] he wants money\n[Misleading or fake]"


@pytest.mark.django_db
def test_the_same_words_again_change_nothing_so_a_double_tap_does_not_grow_the_note():
    reporter, live = person(), listing(person())
    _report(reporter, live.pk, reason_code="other", reason="same words")
    for _ in range(3):
        assert _report(reporter, live.pk, reason_code="other", reason="same words").status_code == 200
    assert ModerationFlag.objects.get().reason == "same words" and ModerationFlag.objects.count() == 1


@pytest.mark.django_db
def test_the_note_never_outgrows_its_limit_and_a_full_note_is_left_alone():
    target = uuid.uuid4()
    reporter = person()
    flag = _flag(reporter, target, reason="x" * 1990)
    reports.add_to_flag(flag, "harassment", "y" * 100)
    flag.refresh_from_db()
    assert len(flag.reason) == reports.NOTE_MAX == 2000 and flag.reason.startswith("x" * 1990)
    full = _flag(person(), target, reason="z" * 2000)
    assert reports.add_to_flag(full, "harassment", "more") is False
    full.refresh_from_db()
    assert full.reason == "z" * 2000


# ---- reviewed is live too -------------------------------------------------------------------------
@pytest.mark.django_db
def test_new_words_on_a_reviewed_flag_put_it_back_in_front_of_a_moderator():
    """`reviewed` used to escape the collapse and open a second row. Now it is one live report, and new information re-opens it."""
    reporter, live = person(), listing(person())
    flag = _flag(reporter, live.pk, status=FlagStatus.REVIEWED)
    res = _report(reporter, live.pk, reason_code="harassment", reason="he did it again")
    flag.refresh_from_db()
    assert res.status_code == 200 and ModerationFlag.objects.count() == 1
    assert flag.status == FlagStatus.OPEN and "he did it again" in flag.reason


@pytest.mark.django_db
def test_nothing_new_leaves_a_reviewed_flag_reviewed():
    reporter, live = person(), listing(person())
    flag = _flag(reporter, live.pk, status=FlagStatus.REVIEWED, reason="same")
    _report(reporter, live.pk, reason_code="harassment", reason="same")
    flag.refresh_from_db()
    assert flag.status == FlagStatus.REVIEWED


@pytest.mark.django_db
@pytest.mark.parametrize("status", [FlagStatus.ACTIONED, FlagStatus.DISMISSED])
def test_a_decided_flag_is_history_and_a_new_report_opens_a_fresh_one(status):
    reporter, live = person(), listing(person())
    _flag(reporter, live.pk, status=status)
    res = _report(reporter, live.pk, reason_code="spam_scam")
    assert res.status_code == 201 and ModerationFlag.objects.filter(status=FlagStatus.OPEN).count() == 1


@pytest.mark.django_db
def test_two_people_reporting_the_same_thing_are_two_flags():
    live = listing(person())
    _report(person(), live.pk, reason_code="spam_scam")
    _report(person(), live.pk, reason_code="spam_scam")
    assert ModerationFlag.objects.count() == 2


# ---- the database enforces it ----------------------------------------------------------------------
@pytest.mark.django_db
def test_the_database_refuses_a_second_live_report_by_the_same_person_about_the_same_thing():
    reporter, target = person(), uuid.uuid4()
    _flag(reporter, target)
    for status in (FlagStatus.OPEN, FlagStatus.REVIEWED):
        with pytest.raises(IntegrityError), transaction.atomic():
            _flag(reporter, target, status=status)


@pytest.mark.django_db
def test_the_constraint_lets_history_and_system_flags_through():
    reporter, target = person(), uuid.uuid4()
    _flag(reporter, target, status=FlagStatus.ACTIONED)
    _flag(reporter, target, status=FlagStatus.DISMISSED)
    _flag(reporter, target)                                     # a live one beside two decided ones is fine
    for _ in range(2):                                          # system flags have no reporter: NULLs are distinct
        ModerationFlag.objects.create(reporter_account=None, target_type="account", target_id=target, reason="rule")
    assert ModerationFlag.objects.count() == 5


@pytest.mark.django_db
def test_two_taps_in_the_same_instant_still_make_one_flag(monkeypatch):
    """The first lookup finds nothing (the other request has not committed), the insert hits the constraint, and the
    request folds into the winner instead of failing or writing a second row."""
    reporter, live = person(), listing(person())
    winner = _flag(reporter, live.pk, reason="the other tap")
    real = reports.first_live
    calls = []

    def blind_once(*args, **kwargs):
        calls.append(1)
        return None if len(calls) == 1 else real(*args, **kwargs)
    monkeypatch.setattr(reports, "first_live", blind_once)

    res = _report(reporter, live.pk, reason_code="harassment", reason="this tap")
    winner.refresh_from_db()
    assert res.status_code == 200 and res.json()["flag_id"] == str(winner.pk)
    assert ModerationFlag.objects.count() == 1 and winner.reason == "the other tap\nthis tap"


# ---- the data migration: flags that already collide ----------------------------------------------------
def _drop_the_constraint():
    """Inside the test's own transaction (Postgres DDL is transactional), so the rollback puts it back."""
    constraint = next(k for k in ModerationFlag._meta.constraints if k.name == reports.LIVE_CONSTRAINT)
    with connection.schema_editor() as editor:
        editor.remove_constraint(ModerationFlag, constraint)
    return constraint


@pytest.mark.django_db
def test_existing_duplicates_are_folded_into_the_oldest_and_the_rest_dismissed():
    reporter, other, target = person(), person(), uuid.uuid4()
    constraint = _drop_the_constraint()
    keeper = _flag(reporter, target, reason="first", code="harassment")
    later_open = _flag(reporter, target, reason="second", code="spam_scam")
    later_reviewed = _flag(reporter, target, status=FlagStatus.REVIEWED, reason="third", code="harassment")
    lone = _flag(other, target, reason="someone else")
    elsewhere = _flag(reporter, uuid.uuid4(), reason="a different thing")
    system = [ModerationFlag.objects.create(reporter_account=None, target_type="account", target_id=target, reason="rule")
              for _ in range(2)]

    folded = reports.merge_live_duplicates(ModerationFlag)

    for f in (keeper, later_open, later_reviewed, lone, elsewhere, *system):
        f.refresh_from_db()
    assert folded == 2
    assert keeper.status == FlagStatus.OPEN and keeper.reason_code == "harassment"
    assert keeper.reason == "first\n[Spam or a scam] second\nthird"
    assert later_open.status == later_reviewed.status == FlagStatus.DISMISSED
    assert later_open.reviewed_by_id is None and later_open.reviewed_at is not None      # nobody is named: a tidy-up, not a decision
    assert (lone.status, elsewhere.status) == (FlagStatus.OPEN, FlagStatus.OPEN)
    assert all(s.status == FlagStatus.OPEN for s in system)
    assert reports.merge_live_duplicates(ModerationFlag) == 0                            # idempotent
    connection.cursor().execute("SET CONSTRAINTS ALL IMMEDIATE")        # the test's own transaction: flush the deferred FK checks
    with connection.schema_editor() as editor:                          # and the constraint can go back on what is left
        editor.add_constraint(ModerationFlag, constraint)


@pytest.mark.django_db
def test_a_group_that_is_all_reviewed_stays_reviewed():
    reporter, target = person(), uuid.uuid4()
    _drop_the_constraint()
    keeper = _flag(reporter, target, status=FlagStatus.REVIEWED, reason="a")
    _flag(reporter, target, status=FlagStatus.REVIEWED, reason="b")
    reports.merge_live_duplicates(ModerationFlag)
    keeper.refresh_from_db()
    assert keeper.status == FlagStatus.REVIEWED and keeper.reason == "a\nb"


def test_the_data_fix_and_the_constraint_are_separate_migrations_in_that_order():
    """Together they fail on exactly the database that needs the fix (Postgres: pending trigger events), so they must not be one."""
    data = import_module("moderation.migrations.0006_fold_duplicate_live_reports")
    constraint = import_module("moderation.migrations.0007_one_live_report_per_person")
    assert [type(op).__name__ for op in data.Migration.operations] == ["RunPython"]
    assert data.Migration.operations[0].reverse_code is not None                # undoing it must not fail
    assert [type(op).__name__ for op in constraint.Migration.operations] == ["AddConstraint"]
    assert ("moderation", "0006_fold_duplicate_live_reports") in constraint.Migration.dependencies
    assert ("moderation", "0005_reason_code_and_account_block") in data.Migration.dependencies


def test_the_note_labels_match_the_apps_wording():
    from moderation.models import ReasonCode
    assert set(reports.LABELS) == set(ReasonCode.values)
    assert reports.LABELS["spam_scam"] == "Spam or a scam" and reports.LABELS["other"] == "Something else"
