"""T3 · the guard: no foreign key to `Account` may exist without an export + erasure decision.

This is the test that would have caught PR-6 and X2. A new table pointing at `account` cannot
quietly escape both the data-rights export and the post-deletion purge — it fails here until
someone classifies it in `accounts/data_map.py`.

Every scan in this project has under-reported at least once (a regex that matched nothing
reads as "all clean"), so the first test proves the scan SEES the tree before the others are
allowed to say anything about what it found.
"""
import pytest
from django.apps import apps

from accounts import export
from accounts.data_map import (
    DATA_MAP,
    ROOT_SECTIONS,
    Delete,
    Entry,
    Excluded,
    Keep,
    Scrub,
    account_foreign_keys,
)


def test_the_scan_sees_the_tree():
    found = account_foreign_keys()

    # Not an exact count (a new table must not break this test), but far too many to be a
    # scan that matched nothing. The three below are spread across three different apps.
    assert len(found) >= 30
    assert ("accounts.AccountIdentity", "account") in found
    assert ("shelter.ShelterProfile", "account") in found
    assert ("sagip.RescueCase", "claimed_by_account") in found


def test_every_foreign_key_to_account_is_classified():
    found = account_foreign_keys()

    unclassified = sorted(found - set(DATA_MAP))
    assert not unclassified, (
        "These foreign keys to Account have no export/erasure decision in "
        f"accounts/data_map.py: {unclassified}")


def test_the_map_has_no_stale_entries():
    stale = sorted(set(DATA_MAP) - account_foreign_keys())
    assert not stale, f"data_map entries for foreign keys that no longer exist: {stale}"


def test_every_entry_is_well_formed():
    for key, entry in DATA_MAP.items():
        assert isinstance(entry, Entry), key
        if isinstance(entry.export, Excluded):
            assert entry.export.reason.strip(), f"{key}: Excluded needs a reason"
        else:
            assert entry.export in export.SECTIONS, f"{key}: unknown export section"
        assert isinstance(entry.erasure, (Delete, Scrub, Keep)), key
        if isinstance(entry.erasure, Keep):
            assert entry.erasure.reason.strip(), f"{key}: Keep needs a reason"
        if isinstance(entry.erasure, Scrub):
            model = apps.get_model(key[0])
            names = {f.name for f in model._meta.get_fields()}
            assert entry.erasure.values, f"{key}: Scrub with nothing to scrub"
            assert set(entry.erasure.values) <= names, f"{key}: Scrub names a missing field"


def test_every_export_section_is_claimed_by_something():
    claimed = {e.export for e in DATA_MAP.values() if not isinstance(e.export, Excluded)}
    orphans = sorted(set(export.SECTIONS) - claimed - ROOT_SECTIONS)
    assert not orphans, f"export sections no table claims (dead code or a missing entry): {orphans}"


@pytest.mark.parametrize("kind", ["Delete", "Scrub", "Keep"])
def test_each_erasure_kind_is_actually_used(kind):
    # If the map degenerated to all-Keep the guard would still pass; this keeps it honest.
    used = {type(e.erasure).__name__ for e in DATA_MAP.values()}
    assert kind in used
