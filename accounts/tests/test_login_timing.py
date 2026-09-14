from unittest.mock import patch

import pytest


@pytest.mark.django_db
def test_unknown_email_still_runs_a_password_hash(client):
    with patch("accounts.views.check_password") as hasher:
        client.post("/api/v1/auth/login", {"email": "nobody@kupkop.invalid", "password": "x"}, content_type="application/json")
    assert hasher.called
