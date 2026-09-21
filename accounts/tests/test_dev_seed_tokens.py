import pytest

from accounts.factories import AccountFactory

URL = "/api/v1/auth/dev/seed_tokens"


@pytest.mark.django_db
def test_debug_off_hides_the_route_entirely(client, settings):
    """In prod (DEBUG=False) this must be indistinguishable from an unknown
    URL: same status, no serializer output, no hint the route exists."""
    settings.DEBUG = False
    AccountFactory(email="prod@example.com")

    res = client.post(URL, {"email": "prod@example.com"}, content_type="application/json")
    unknown = client.post("/api/v1/auth/dev/this-route-does-not-exist",
                           {"email": "prod@example.com"}, content_type="application/json")

    assert res.status_code == 404
    assert unknown.status_code == 404
    assert res.content == unknown.content


@pytest.mark.django_db
def test_debug_on_valid_email_returns_tokens_and_account(client, settings):
    settings.DEBUG = True
    account = AccountFactory(email="seedme@example.com", display_name="Seed Me")

    res = client.post(URL, {"email": "SeedMe@Example.com"}, content_type="application/json")

    assert res.status_code == 200
    body = res.json()
    assert body["access"]
    assert body["refresh"]
    assert body["account"]["email"] == account.email


@pytest.mark.django_db
def test_debug_on_unknown_email_returns_404_with_detail(client, settings):
    settings.DEBUG = True

    res = client.post(URL, {"email": "nobody@example.com"}, content_type="application/json")

    assert res.status_code == 404
    assert res.json() == {"detail": "account not found"}


@pytest.mark.django_db
def test_debug_on_non_post_returns_405(client, settings):
    settings.DEBUG = True

    res = client.get(URL)

    assert res.status_code == 405


@pytest.mark.django_db
def test_debug_on_missing_body_returns_400_with_detail(client, settings):
    settings.DEBUG = True

    res = client.post(URL, {}, content_type="application/json")

    assert res.status_code == 400
    assert res.json() == {"detail": "email required"}


@pytest.mark.django_db
def test_debug_on_invalid_json_returns_400_with_detail(client, settings):
    settings.DEBUG = True

    res = client.post(URL, data="not json", content_type="application/json")

    assert res.status_code == 400
    assert res.json() == {"detail": "email required"}
