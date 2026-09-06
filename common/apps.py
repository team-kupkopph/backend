from django.apps import AppConfig


class CommonConfig(AppConfig):
    """US-E2 · the one place error reporting is started.

    `ready()` rather than settings, because settings should describe configuration and not
    perform side effects — and because this must run once per process, after settings are
    fully loaded, which is exactly what an AppConfig guarantees.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "common"

    def ready(self):
        # No-op unless SENTRY_DSN is set (the FCM/S3 seam posture), so this is silent in
        # development and in every test run.
        from common.observability import init_sentry
        init_sentry()
