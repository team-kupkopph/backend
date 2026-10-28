"""A profile photo is uploaded, not linked (account/settings review, Task 10's missing half).

`PATCH /me` took `photo_file_url` as any string and stored it. The photo is shown to OTHER people
(stories, comments, the You tab), so "any string" is an arbitrary URL rendered in their app: a
tracking pixel, or someone else's file. The rule is the one the shelter logo already follows
(`common/uploads.own_upload_url_error`): the URL must be THIS account's own `profile_photo` upload.
Replacing or removing the photo also deletes the old object, because an avatar is personal data.
"""
import json
import uuid

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for


def _auth(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


def _url(account, purpose="profile_photo", owner=None):
    return f"https://bkt.example/{purpose}/{(owner or account).pk}/{uuid.uuid4()}"


def _patch(client, account, body):
    return client.patch("/api/v1/me", body, content_type="application/json", **_auth(account))


@pytest.mark.django_db
def test_profile_photo_is_a_public_upload_purpose(client):
    account = AccountFactory(email_verified_at=timezone.now())

    res = client.post("/api/v1/media/presign", {"purpose": "profile_photo", "content_type": "image/png"},
                      content_type="application/json", **_auth(account))

    assert res.status_code == 200, res.content
    assert f"profile_photo/{account.pk}/" in json.dumps(res.json())


@pytest.mark.django_db
def test_the_photo_must_be_this_accounts_own_profile_photo_upload(client):
    account, other = AccountFactory(email_verified_at=timezone.now()), AccountFactory(email_verified_at=timezone.now())

    ok = _patch(client, account, {"photo_file_url": _url(account)})
    theirs = _patch(client, account, {"photo_file_url": _url(account, owner=other)})
    wrong_purpose = _patch(client, account, {"photo_file_url": _url(account, purpose="listing_photo")})
    tracker = _patch(client, account, {"photo_file_url": "https://tracker.example/pixel.gif?u=1"})
    junk = _patch(client, account, {"photo_file_url": "javascript:alert(1)"})

    assert ok.status_code == 200 and ok.json()["photo_url"].startswith("https://bkt.example/profile_photo/")
    for res in (theirs, wrong_purpose, tracker, junk):
        assert res.status_code == 400
        assert "photo_file_url" in res.json()["error"]["details"]
    account.refresh_from_db()
    assert account.photo_url.startswith("https://bkt.example/profile_photo/")   # the refusals changed nothing


@pytest.mark.django_db
def test_a_blank_photo_removes_it(client):
    account = AccountFactory(email_verified_at=timezone.now(), photo_url=_url(AccountFactory()))

    res = _patch(client, account, {"photo_file_url": ""})

    assert res.status_code == 200 and res.json()["photo_url"] in ("", None)
    account.refresh_from_db()
    assert account.photo_url == ""


@pytest.mark.django_db
def test_replacing_or_removing_the_photo_deletes_the_old_object(client, monkeypatch):
    deleted = []
    monkeypatch.setattr("accounts.views.delete_object", lambda url, visibility=None: deleted.append(url))
    account = AccountFactory(email_verified_at=timezone.now())
    first, second = _url(account), _url(account)
    account.photo_url = first
    account.save()

    _patch(client, account, {"photo_file_url": second})
    _patch(client, account, {"photo_file_url": ""})

    assert deleted == [first, second]


@pytest.mark.django_db
def test_a_provider_avatar_is_not_ours_to_delete(client, monkeypatch):
    """A social sign-in stores the provider's picture URL. Replacing it must not try to delete an
    object in OUR bucket by a key derived from someone else's URL."""
    deleted = []
    monkeypatch.setattr("accounts.views.delete_object", lambda url, visibility=None: deleted.append(url))
    account = AccountFactory(email_verified_at=timezone.now(), photo_url="https://lh3.googleusercontent.com/a/xyz")

    res = _patch(client, account, {"photo_file_url": _url(account)})

    assert res.status_code == 200 and deleted == []


@pytest.mark.django_db
def test_saving_the_same_photo_again_deletes_nothing(client, monkeypatch):
    deleted = []
    monkeypatch.setattr("accounts.views.delete_object", lambda url, visibility=None: deleted.append(url))
    account = AccountFactory(email_verified_at=timezone.now())
    url = _url(account)
    account.photo_url = url
    account.save()

    res = _patch(client, account, {"photo_file_url": url})

    assert res.status_code == 200 and deleted == []


@pytest.mark.django_db
def test_changing_only_the_name_leaves_the_photo_alone(client):
    account = AccountFactory(email_verified_at=timezone.now())
    url = _url(account)
    account.photo_url = url
    account.save()

    _patch(client, account, {"display_name": "New Name"})

    account.refresh_from_db()
    assert account.photo_url == url
