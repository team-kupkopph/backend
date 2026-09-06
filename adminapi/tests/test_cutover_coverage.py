"""US-X1 · prove the console replaces the Django admin, before US-X2 switches it off.

⚠️ THIS TEST DERIVES THE REGISTRY AT RUNTIME. It does not read the table in
`dev/sprint-10-stories.md`, and it must never be changed to.

That table was measured on 2026-09-06. If anyone registers another model between then and
cutover, a table-reading check passes while the console is silently missing something on the
day it becomes the only surface. Deriving from `admin.site._registry` is the whole point:
the check breaks when someone adds an admin, which is exactly when it should.

> Across Sprint 8 every source-scan guard in this project under-reported at least once, and
> four of five times printed a plausible SMALLER number rather than an error. The nastiest
> shape was a count that went DOWN, which in a cleanup sprint reads as progress.
"""
import pytest
from django.contrib import admin


def registered():
    """Every model registered on the live admin site, as `app_label.ModelName`."""
    return {f"{m._meta.app_label}.{m.__name__}" for m in admin.site._registry}


# Every registered model must appear in EXACTLY ONE of the two maps below. A model in neither
# fails the test; that is the mechanism.

# Replaced by a console route. The story is recorded so the claim is auditable later.
CONSOLE_REPLACEMENT = {
    "verifications.VerificationRequest": ("/verifications", "Sprint 9 · US-C1–C4"),
    "moderation.ModerationFlag": ("/moderation", "Sprint 10 · US-M1–M3"),
    "shelter.DonationQr": ("/donations", "Sprint 10 · US-Q1–Q2"),
    "auth.User": ("/settings", "Sprint 10 · US-T1–T2"),
    "auth.Group": ("/settings", "Sprint 10 · US-T1 (roles are Groups)"),
    "otp_totp.TOTPDevice": ("/settings", "Sprint 10 · US-T1 (enrol + reset 2FA)"),
}

# Registered, but deliberately NOT given a console route — with the reason, because
# "we decided not to" and "we forgot" are indistinguishable a year later.
NOT_REPLACED = {
    "token_blacklist.OutstandingToken":
        "SimpleJWT's own bookkeeping. Rows are written by the token machinery and read by it; "
        "nobody administers them by hand, and the console's own logout/rotation already "
        "manages the lifecycle. Readable through the model browser if ever needed.",
    "token_blacklist.BlacklistedToken":
        "Same as OutstandingToken — SimpleJWT internals, not an ops surface.",
}


def test_the_registry_scan_found_something():
    """⚠️ A zero here must never read as 'everything is replaced'.

    This is the assertion that separates 'the console covers the admin' from 'the
    introspection broke and returned an empty set'.
    """
    found = registered()
    assert len(found) >= 6, f"expected the admin to have registrations, found {found}"


def test_every_registered_model_has_a_decision():
    """The mechanism: a newly registered model with no mapping FAILS."""
    unaccounted = registered() - set(CONSOLE_REPLACEMENT) - set(NOT_REPLACED)
    assert not unaccounted, (
        f"model(s) registered in the Django admin with no console route and no recorded "
        f"reason: {sorted(unaccounted)}. Add a console route and map it in "
        f"CONSOLE_REPLACEMENT, or record why it needs none in NOT_REPLACED. "
        f"US-X2 must not proceed until this passes."
    )


def test_the_maps_do_not_describe_models_that_are_gone():
    """Ratchet the other way: a stale entry would keep passing the check above while quietly
    exempting whatever later takes that name."""
    known = registered()
    stale = (set(CONSOLE_REPLACEMENT) | set(NOT_REPLACED)) - known
    assert not stale, f"map(s) name unregistered model(s): {sorted(stale)}"


def test_the_buckets_balance():
    """converted + not-replaced === total. Nothing may fall out of both."""
    found = registered()
    replaced = len(found & set(CONSOLE_REPLACEMENT))
    excused = len(found & set(NOT_REPLACED))
    assert replaced + excused == len(found), (
        f"{replaced} + {excused} != {len(found)} — a registration fell out of every bucket"
    )


@pytest.mark.django_db
def test_every_claimed_console_route_actually_exists():
    """⚠️ A map claiming '/moderation' proves nothing if that route 404s.

    Without this the coverage table is a promise rather than evidence — which is exactly the
    failure mode US-X1 exists to prevent.
    """
    from django.urls import get_resolver

    api_paths = set()
    for pattern in get_resolver().url_patterns:
        if str(getattr(pattern, "pattern", "")) != "admin-api/":
            continue
        for sub in pattern.url_patterns:
            api_paths.add(str(sub.pattern))
    assert api_paths, "the URLconf scan found no /admin-api/ routes — broken, not clean"

    # Console path -> the backing endpoint that makes it work.
    BACKING = {
        "/verifications": "verifications",
        "/moderation": "flags",
        "/donations": "donation-qrs",
        "/settings": "staff",
    }
    missing = []
    for model, (route, _story) in CONSOLE_REPLACEMENT.items():
        endpoint = BACKING.get(route)
        assert endpoint is not None, f"{route} has no backing endpoint recorded"
        if endpoint not in api_paths:
            missing.append(f"{model} claims {route}, but /admin-api/{endpoint} does not exist")
    assert not missing, "\n    ".join(missing)


def test_the_story_hiding_behaviour_survives_the_admin():
    """⚠️ Model coverage is NOT behaviour coverage, and this is the case that proved it.

    "Actioning a story flag hides the story" (US-T3 / D-S6-4) lived only inside
    ModerationFlagAdmin._resolve — the file US-X2 deletes. A registry check would have said
    ModerationFlag was covered while the behaviour went with the admin. It now lives in
    moderation/actions.py, and this asserts it is not back in the admin.
    """
    import inspect

    from moderation import actions, admin as moderation_admin

    assert hasattr(actions, "resolve_flag"), "the shared decision service is gone"
    assert "StoryStatus.HIDDEN" in inspect.getsource(actions.resolve_flag), (
        "story-hiding is no longer in the shared service — it must not live only in a UI layer"
    )
    admin_source = inspect.getsource(moderation_admin)
    assert "StoryStatus" not in admin_source, (
        "story-hiding has reappeared inside moderation/admin.py, the file US-X2 deletes"
    )
