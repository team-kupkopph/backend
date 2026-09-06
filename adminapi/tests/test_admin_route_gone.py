"""US-X2 · /admin/ is gone. Asserted over HTTP, not just in the URLconf."""
import pytest


@pytest.mark.django_db
def test_admin_index_404s(client):
    assert client.get("/admin/").status_code == 404


@pytest.mark.django_db
def test_admin_login_404s(client):
    """The login page is the one a bookmark lands on."""
    assert client.get("/admin/login/").status_code == 404


@pytest.mark.django_db
def test_a_model_change_list_404s(client, staffer):
    """Even authenticated as staff — the surface is gone, not merely permission-gated."""
    client.force_login(staffer)
    assert client.get("/admin/verifications/verificationrequest/").status_code == 404


@pytest.mark.django_db
def test_the_console_api_still_works(client, staffer):
    """The other half of the cutover: ops did not lose a surface, it moved."""
    from adminapi.tests.conftest import full_signin
    token = full_signin(client, staffer)["access"]
    res = client.get("/admin-api/verifications", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert res.status_code == 200


@pytest.mark.django_db
def test_the_admin_classes_are_kept_for_rollback(client):
    """⚠️ Unregistered, NOT deleted. Reverting the US-X2 commit must restore a working admin,
    which it cannot do if the ModelAdmin classes went with it."""
    from moderation.admin import ModerationFlagAdmin
    from shelter.admin import DonationQrAdmin
    from verifications.admin import VerificationRequestAdmin

    for cls in (VerificationRequestAdmin, DonationQrAdmin, ModerationFlagAdmin):
        assert cls is not None
