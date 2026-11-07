"""Deploy-time warnings for moderation settings that are safe to leave blank in development and wrong to leave blank in production.

Run with `python manage.py check --deploy`. A warning, never an error: a platform with nobody on call yet is a legitimate state, and
refusing to boot over it would be worse than the thing it warns about. What it refuses to do is let the gap be silent."""
from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security, deploy=True)
def alert_recipients_are_set(app_configs, **kwargs):
    """Review step 23 (MQ8) · the overdue-report alert emails MODERATION_ALERT_EMAILS. Blank, nobody is told a report is late (the sweep
    logs a warning every hour instead), which is exactly the failure the alert exists to prevent."""
    if settings.DEBUG or settings.MODERATION_ALERT_EMAILS:
        return []
    return [Warning(
        "MODERATION_ALERT_EMAILS is empty: nobody will be told when a report passes its response target.",
        hint="Set it to a comma-separated list of monitored addresses (the on-call moderator, plus a lead).",
        id="moderation.W001")]


@register(Tags.security, deploy=True)
def terms_gate_matches_the_version(app_configs, **kwargs):
    """Review step 7a · TERMS_ENFORCED asks members to accept TERMS_VERSION before they post. Switching it on with the version that signup
    already records asks nobody anything, which is almost certainly a mistake: the point of the gate is a NEW document."""
    if not settings.TERMS_ENFORCED:
        return []
    return [Warning(
        f"TERMS_ENFORCED is on (TERMS_VERSION={settings.TERMS_VERSION}). Members whose accepted version differs will be asked to accept it "
        "the next time they post.",
        hint="Check that TERMS_VERSION is the version of the text counsel approved, and that mobile_app/src/consent.ts says the same.",
        id="moderation.W002")]
