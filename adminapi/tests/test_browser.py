"""US-V1 · the read-only model browser."""
import pytest
from django.contrib.auth.models import Group

from accounts.factories import AccountFactory
from adminapi.browser import MAX_PAGE, SAFE_FIELDS
from adminapi.tests.conftest import full_signin
from verifications.models import (
    VerificationAccessLog,
    VerificationDocument,
    VerificationRequest,
)


@pytest.fixture
def auth(client, staffer):
    staffer.groups.add(Group.objects.get(name="superadmin"))
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


# -- the read-only contract ---------------------------------------------------------------
def test_no_write_route_exists_under_models():
    """⚠️ Asserting the ABSENCE of a route is the only way to keep it absent. A generic form
    that writes any row bypasses notify(), the concurrency guards and reviewed_by — it would
    approve a shelter without telling it."""
    import adminapi.urls as urls
    from adminapi import browser_views

    model_routes = [p for p in urls.urlpatterns if str(p.pattern).startswith("models")]
    assert model_routes, "the scan found no model routes — broken, not clean"
    for route in model_routes:
        cls = route.callback.cls
        for verb in ("post", "put", "patch", "delete"):
            assert not hasattr(cls, verb), f"{cls.__name__} exposes {verb.upper()}"
    # And nothing in the module defines one for later.
    for name in dir(browser_views):
        cls = getattr(browser_views, name)
        if isinstance(cls, type) and name.endswith("View"):
            for verb in ("post", "put", "patch", "delete"):
                assert not hasattr(cls, verb), f"{name} exposes {verb.upper()}"


# -- role gating ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_reviewer_cannot_browse(client, staffer):
    staffer.groups.add(Group.objects.get(name="reviewer"))
    token = full_signin(client, staffer)["access"]
    assert client.get("/admin-api/models", HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 403


# -- the allow-lists ------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_registry_is_non_empty_and_resolves(client, auth):
    """A zero here would read as 'nothing to browse' rather than 'the registry broke'."""
    results = client.get("/admin-api/models", **auth).json()["results"]
    assert len(results) >= 15
    assert all(r["field_count"] > 0 for r in results)


@pytest.mark.django_db
def test_a_model_absent_from_the_allow_list_is_not_browsable(client, auth):
    """Fails CLOSED: a model added next sprint is invisible until someone lists it."""
    res = client.get("/admin-api/models/auth.User", **auth)
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_browsable"


@pytest.mark.django_db
def test_password_hash_is_never_returned(client, auth):
    """The one field this whole design exists to keep out."""
    AccountFactory(email="someone@ex.com", password="hunter2hunter2")
    body = client.get("/admin-api/models/accounts.Account", **auth).json()
    assert "password_hash" not in body["fields"]
    blob = str(body["results"])
    assert "pbkdf2" not in blob and "password" not in blob


def test_no_allow_list_contains_a_known_secret_field():
    """A field-level guard over the registry itself, so a future edit cannot add one quietly."""
    banned = {"password_hash", "password", "fcm_token", "file_url", "qr_image_url"}
    offenders = {f"{a}.{m}.{f}" for (a, m), fields in SAFE_FIELDS.items()
                 for f in fields if f in banned}
    assert not offenders, f"secret field(s) in an allow-list: {offenders}"


@pytest.mark.django_db
def test_the_document_file_url_is_not_browsable(client, auth):
    """A signed URL to a government ID must come from the review screen that logs it, never
    from a generic table view."""
    body = client.get("/admin-api/models/verifications.VerificationDocument", **auth).json()
    assert "file_url" not in body["fields"]


# -- the access log is not bypassable --------------------------------------------------------
@pytest.mark.django_db
def test_browsing_documents_writes_an_access_log_row(client, auth, staffer):
    """⚠️ The browser is not a way around RA 10173 accountability."""
    vr = VerificationRequest.objects.create(account=AccountFactory(), type="shelter_org")
    VerificationDocument.objects.create(verification=vr, doc_type="gov_id", file_url="s3://x")
    VerificationAccessLog.objects.all().delete()

    client.get("/admin-api/models/verifications.VerificationDocument", **auth)
    log = VerificationAccessLog.objects.get()
    assert log.verification_id == vr.verification_id
    assert log.staff_username == staffer.get_username()


@pytest.mark.django_db
def test_browsing_something_else_writes_no_access_log(client, auth):
    VerificationAccessLog.objects.all().delete()
    client.get("/admin-api/models/accounts.Account", **auth)
    assert VerificationAccessLog.objects.count() == 0


# -- paging --------------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_page_size_is_capped(client, auth):
    for _ in range(5):
        AccountFactory()
    body = client.get(f"/admin-api/models/accounts.Account?limit={MAX_PAGE * 10}", **auth).json()
    assert len(body["results"]) <= MAX_PAGE


@pytest.mark.django_db
def test_a_single_row_is_readable(client, auth):
    account = AccountFactory(display_name="Findable")
    body = client.get(f"/admin-api/models/accounts.Account/{account.account_id}", **auth).json()
    assert body["row"]["display_name"] == "Findable"


@pytest.mark.django_db
def test_search_only_covers_fields_that_are_returned(client, auth):
    """Otherwise the browser becomes an oracle for values it refuses to show."""
    AccountFactory(display_name="Searchable Person", email="searchme@ex.com")
    hits = client.get("/admin-api/models/accounts.Account?q=Searchable", **auth).json()["results"]
    assert any(r["display_name"] == "Searchable Person" for r in hits)
