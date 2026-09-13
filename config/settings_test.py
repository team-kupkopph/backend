"""Settings for the test run: `config.settings`, with every outbound seam blanked.

`config/settings.py` loads `.env`, and each seam below is env-sourced and credential-guarded
— set the variable and the real backend runs. That is the right posture for a checkout and
the wrong one for a test process: with `EMAIL_PROVIDER=resend` in a developer's `.env`,
every signup and resend test posted to api.resend.com, and CI never noticed because CI has
no `.env`. A test suite that behaves differently per developer is not one suite.

This module exists (rather than a fixture) because the seams have to be blank BEFORE any
app's `ready()` runs — `common.apps` calls `init_sentry()` there, and a per-test fixture is
hours too late for that. Selected by `pytest.ini`; held in place by
common/tests/test_no_outbound_in_tests.py.

Tests that exercise a provider opt in explicitly, per test, with `@override_settings(...)` —
see common/tests/test_senders.py — and patch the client so nothing leaves the process.
"""
from config.settings import *  # noqa: F401,F403 — this IS config.settings, minus the seams

# Mail. Blank provider → ConsoleSender (common/senders.py::get_sender).
EMAIL_PROVIDER = ""
RESEND_API_KEY = ""
AWS_SES_REGION = ""
# Error reporting. Blank DSN → init_sentry() is a no-op.
SENTRY_DSN = ""
# Media. Blank buckets → common/storage.py's local stub.
MEDIA_S3_BUCKET_PUBLIC = ""
MEDIA_S3_BUCKET_RESTRICTED = ""
# Push. Blank project → notifications/push.py's no-op transport.
FCM_PROJECT_ID = ""
FCM_CREDENTIALS_PATH = ""
