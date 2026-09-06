"""A token this API cannot read must be a 401, never a 500.

`AccountJWTAuthentication` indexed `validated_token["account_id"]`. Nothing guarantees the
claim is there — US-K3 removed SIMPLE_JWT's USER_ID_CLAIM, and since US-B1 the platform-ops
console mints staff tokens that deliberately carry `staff_user_id`/`admin_account_id`
instead. Indexing turned any such token into an uncaught KeyError: a 500 with a traceback in
the logs, on an unauthenticated endpoint, triggerable by anyone holding any other token.
"""
import pytest
from rest_framework_simplejwt.tokens import RefreshToken


def _token_without_account_id():
    refresh = RefreshToken()
    refresh["staff_user_id"] = 1
    return str(refresh.access_token)


@pytest.mark.django_db
def test_a_token_without_account_id_is_401_not_500(client):
    res = client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {_token_without_account_id()}")
    assert res.status_code == 401, f"expected 401, got {res.status_code}: {res.content!r}"
