import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import AccountStatus


@pytest.mark.django_db
def test_suspended_account_gets_the_same_401_as_a_wrong_password(client):
    acc = AccountFactory(email="sus@kupkop.invalid", email_verified_at=timezone.now(), status=AccountStatus.SUSPENDED)
    acc.set_password("Right1234"); acc.save()
    # Pin X-Request-ID so RequestIdMiddleware (common/observability.py) echoes the SAME id
    # into both error bodies instead of minting a fresh uuid per request — otherwise the
    # byte-identical comparison below would fail on that unrelated tracing field even when
    # the suspension check itself introduces no oracle.
    headers = {"HTTP_X_REQUEST_ID": "test-fixed-request-id"}
    ok = client.post("/api/v1/auth/login", {"email": "sus@kupkop.invalid", "password": "Right1234"},
                      content_type="application/json", **headers)
    wrong = client.post("/api/v1/auth/login", {"email": "sus@kupkop.invalid", "password": "Wrong1234"},
                         content_type="application/json", **headers)
    assert ok.status_code == 401
    assert ok.json() == wrong.json()          # byte-identical body: no suspension oracle
