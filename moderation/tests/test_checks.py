"""Owner items · the deploy checks that make a blank moderation setting loud instead of silent.

`manage.py check --deploy` is where an operator looks before a release. Both checks are WARNINGS: a platform with nobody on call yet is a
legitimate state, and refusing to boot over it would be worse. What they refuse is to let the gap go unmentioned."""
from django.test import override_settings

from moderation import checks


def _ids(found):
    return [w.id for w in found]


@override_settings(DEBUG=False, MODERATION_ALERT_EMAILS=[])
def test_a_production_with_nobody_to_alert_is_warned():
    found = checks.alert_recipients_are_set(None)
    assert _ids(found) == ["moderation.W001"] and "nobody will be told" in found[0].msg


@override_settings(DEBUG=False, MODERATION_ALERT_EMAILS=["ops@example.org"])
def test_recipients_set_means_no_warning():
    assert checks.alert_recipients_are_set(None) == []


@override_settings(DEBUG=True, MODERATION_ALERT_EMAILS=[])
def test_development_is_not_nagged():
    assert checks.alert_recipients_are_set(None) == []


@override_settings(TERMS_ENFORCED=False)
def test_the_terms_check_is_silent_while_the_gate_is_off():
    assert checks.terms_gate_matches_the_version(None) == []


@override_settings(TERMS_ENFORCED=True, TERMS_VERSION="2026-12-01")
def test_turning_the_gate_on_names_the_version_members_will_be_asked_to_accept():
    found = checks.terms_gate_matches_the_version(None)
    assert _ids(found) == ["moderation.W002"] and "2026-12-01" in found[0].msg


def test_both_checks_are_registered_as_deploy_checks():
    """`check --deploy` only runs deployment checks; a check registered the ordinary way would run on every start and nag development."""
    from django.core.checks.registry import registry
    assert {"alert_recipients_are_set", "terms_gate_matches_the_version"} <= {c.__name__ for c in registry.deployment_checks}
    assert not {"alert_recipients_are_set", "terms_gate_matches_the_version"} & {c.__name__ for c in registry.registered_checks}
