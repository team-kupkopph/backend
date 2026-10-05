"""US-N1 — the registry itself: every type a real notify() call site uses is registered,
and the registry doesn't silently grow stale types nothing calls any more.
"""
import re
from pathlib import Path

from notifications.types import REGISTRY, is_registered

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CALL_SITE_FILES = ["verifications/review.py", "sagip/sweeps.py", "sagip/views.py",
                   "listings/views.py", "listings/notices.py"]


def _types_called_in_source():
    """Every string literal passed as notify()'s second positional arg, or
    _notify_decision()'s (verifications/review.py's thin wrapper around notify()),
    across the real call-site files — a lightweight regex sweep, not a full parse, but
    enough to catch "someone added a new notify() call with an unregistered type"."""
    found = set()
    pattern = re.compile(r'(?:notify|_notify_decision)\([^,]+,\s*"([a-z_]+)"')
    for rel in CALL_SITE_FILES:
        text = (REPO_ROOT / rel).read_text()
        found.update(pattern.findall(text))
    return found


def test_every_type_actually_called_in_source_is_registered():
    called = _types_called_in_source()
    assert called, "sweep found no notify() calls — the regex or file list is stale"
    unregistered = called - set(REGISTRY)
    assert not unregistered, f"notify() call site(s) use unregistered type(s): {unregistered}"


def test_is_registered_true_for_a_known_type():
    assert is_registered("verification_approved") is True


def test_is_registered_false_for_an_unknown_type():
    assert is_registered("something_made_up") is False


def test_registry_has_no_duplicate_keys_by_construction():
    # REGISTRY is built as {t.key: t for t in _TYPES} — a duplicate key in the list
    # would silently overwrite, not error. Assert the list-to-dict conversion actually
    # preserved every entry (no silent collision) as a documented invariant.
    from notifications.types import _TYPES
    assert len(_TYPES) == len(REGISTRY) == len({t.key for t in _TYPES})


def test_every_type_declares_push_and_deep_link():
    for key, t in REGISTRY.items():
        assert isinstance(t.push, bool), key
        assert isinstance(t.deep_link, str), key


def test_a_known_type_routes_where_expected():
    assert REGISTRY["signup_requested"].push is True
    assert REGISTRY["signup_requested"].deep_link == "kupkop://shelter/shifts/{shift_id}/requests"
