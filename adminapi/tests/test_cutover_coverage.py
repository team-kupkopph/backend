"""US-X1/X2 · the cutover, and what keeps holding after it.

⚠️ POST-CUTOVER (2026-09-06). US-X2 removed `path("admin/", admin.site.urls)`, so the three
domain ModelAdmins are unregistered and the registry now holds only third-party entries. The
checks below changed meaning with it — deliberately, and each says how — rather than being
loosened until they passed again.

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


def test_the_admin_route_is_gone():
    """US-X2, asserted directly. This is the cutover."""
    from django.urls import get_resolver

    prefixes = {str(getattr(p, "pattern", "")) for p in get_resolver().url_patterns}
    assert prefixes, "the URLconf scan found nothing — broken, not clean"
    assert "admin/" not in prefixes, "the Django admin route is back"


def test_contrib_admin_is_still_installed():
    """⚠️ Removing the APP would drop django_admin_log and the admin's own migrations — a
    schema change nobody asked for. US-X2 removes the ROUTE only, and this pins that."""
    from django.conf import settings

    assert "django.contrib.admin" in settings.INSTALLED_APPS


def test_the_registry_scan_found_something():
    """⚠️ A zero here must never read as 'everything is replaced'.

    Threshold lowered from 6 to 2 by US-X2, because the three domain admins are now
    unregistered — NOT because the check was failing. `django.contrib.auth` always registers
    User and Group, so an empty registry still means the introspection broke rather than that
    everything is covered.
    """
    found = registered()
    assert len(found) >= 2, f"expected contrib registrations at minimum, found {found}"


def test_every_registered_model_has_a_decision():
    """The mechanism: a newly registered model with no mapping FAILS."""
    unaccounted = registered() - set(CONSOLE_REPLACEMENT) - set(NOT_REPLACED)
    assert not unaccounted, (
        f"model(s) registered in the Django admin with no console route and no recorded "
        f"reason: {sorted(unaccounted)}. Add a console route and map it in "
        f"CONSOLE_REPLACEMENT, or record why it needs none in NOT_REPLACED. "
        f"US-X2 must not proceed until this passes."
    )


def test_the_replacement_map_is_kept_as_the_historical_record():
    """⚠️ This test INVERTED at cutover, and that is the honest change rather than deleting it.

    Before US-X2 it asserted no map entry named an unregistered model — a stale entry would
    have quietly exempted whatever later took that name. After US-X2 the three domain models
    are unregistered BY DESIGN, so that assertion would now fail for the right reason.

    What still matters is that the record of what replaced what survives, and that the entries
    for the models we actually retired are still there to be read.
    """
    for model in ("verifications.VerificationRequest", "moderation.ModerationFlag",
                  "shelter.DonationQr"):
        assert model in CONSOLE_REPLACEMENT, f"{model}'s replacement record was lost"
        route, story = CONSOLE_REPLACEMENT[model]
        assert route.startswith("/") and story, f"{model} has an incomplete record"


def test_the_buckets_balance():
    """converted + not-replaced === total, for whatever is registered NOW. Still the guard
    against a new registration falling out of every bucket."""
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


def test_per_document_rejection_exists_outside_the_admin():
    """⚠️ The SECOND behaviour that lived only in the surface US-X2 deletes.

    `verifications/admin.py::needs_info` rejected individual documents with their own reasons
    (US-R6) and then bounced the request. The console's needs-info did only the second half,
    so switching the admin off would have removed the only way to tell an applicant WHICH file
    to replace — while a registry check reported `VerificationRequest` as fully covered.

    Found by reading the admin's write paths before the switch-off, not by any automated
    check. That is the argument for doing so again next time.
    """
    import inspect

    from adminapi import verifications_views

    cls = verifications_views.VerificationDecisionView
    assert hasattr(cls, "_reject_documents"), "the per-document rejection helper is gone"

    # ⚠️ The helper EXISTING is not the property — it has to be CALLED from the decision path.
    # A first version of this test asserted only that the symbol was present, and a mutation
    # that deleted the call site sailed straight through it. Assert the wiring.
    post_source = inspect.getsource(cls.post)
    assert "_reject_documents(" in post_source, (
        "per-document rejection is defined but never called — US-R6 parity is gone in practice"
    )

    # The behavioural proof lives in test_verification_decisions.py
    # (test_needs_info_can_reject_individual_documents); this is the cutover-audit marker that
    # says WHY it must keep passing.


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
