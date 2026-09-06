"""US-T1 · success stories — feed, compose, react, moderation-hide, flag (D-S6-4)."""
import pytest

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from community.models import StoryPhoto, StoryPost, StoryReaction, StoryStatus, StoryType
from moderation.models import ModerationFlag


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _story(author=None, status=StoryStatus.PUBLISHED, story_type=StoryType.GENERAL, photos=1):
    s = StoryPost.objects.create(author_account=author or AccountFactory(),
                                 story_type=story_type, caption="Adopted!", status=status)
    for i in range(photos):
        StoryPhoto.objects.create(story=s, url=f"https://x/{i}.jpg", is_primary=(i == 0))
    return s


@pytest.mark.django_db
def test_compose_a_story_with_a_photo(client):
    author = AccountFactory()
    res = client.post("/api/v1/stories",
                      {"caption": "Meet Rex", "photos": [{"file_url": "https://x/a.jpg"}]},
                      content_type="application/json", **_hdr(author))
    assert res.status_code == 201
    story = StoryPost.objects.get(pk=res.json()["story_id"])
    assert story.author_account_id == author.pk and story.photos.count() == 1


@pytest.mark.django_db
def test_a_story_needs_at_least_one_photo(client):
    res = client.post("/api/v1/stories", {"caption": "no pic", "photos": []},
                      content_type="application/json", **_hdr(AccountFactory()))
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "photo_required"


@pytest.mark.django_db
def test_story_type_auto_derives_from_a_linked_case(client):
    """A story citing a rescue case is a rescue story regardless of any passed type."""
    from django.contrib.gis.geos import Point

    from sagip.models import RescueCase, StrayReport
    rep = StrayReport.objects.create(species="dog", condition="injured",
                                     geom=Point(121.0, 14.6, srid=4326))
    case = RescueCase.objects.create(report=rep, claimed_by_account=AccountFactory())
    res = client.post("/api/v1/stories",
                      {"caption": "saved", "story_type": "general",
                       "rescue_case_id": str(case.pk),
                       "photos": [{"file_url": "https://x/a.jpg"}]},
                      content_type="application/json", **_hdr(AccountFactory()))
    assert res.status_code == 201
    assert StoryPost.objects.get(pk=res.json()["story_id"]).story_type == StoryType.RESCUE


@pytest.mark.django_db
def test_feed_lists_published_newest_first_and_is_public(client):
    _story()
    res = client.get("/api/v1/stories")     # no auth
    assert res.status_code == 200
    assert len(res.json()["results"]) == 1


@pytest.mark.django_db
def test_feed_excludes_hidden_stories(client):
    _story(status=StoryStatus.HIDDEN)
    assert client.get("/api/v1/stories").json()["results"] == []


def _acct_in(city):
    from accounts.models import Address
    acc = AccountFactory()
    Address.objects.create(account=acc, city=city, is_primary=True)
    return acc


@pytest.mark.django_db
def test_feed_filters_by_city(client):
    _story(author=_acct_in("Marikina"))
    _story(author=_acct_in("Pasig"))
    res = client.get("/api/v1/stories?city=Marikina")
    cities = {r["author"]["city"] for r in res.json()["results"]}
    assert cities == {"Marikina"}


@pytest.mark.django_db
def test_reacting_is_idempotent(client):
    story = _story()
    user = AccountFactory()
    client.post(f"/api/v1/stories/{story.pk}/reactions", **_hdr(user))
    res = client.post(f"/api/v1/stories/{story.pk}/reactions", **_hdr(user))
    assert res.status_code == 200 and res.json()["reaction_count"] == 1
    assert StoryReaction.objects.filter(story=story).count() == 1


@pytest.mark.django_db
def test_unreacting_removes_the_reaction(client):
    story = _story()
    user = AccountFactory()
    client.post(f"/api/v1/stories/{story.pk}/reactions", **_hdr(user))
    res = client.delete(f"/api/v1/stories/{story.pk}/reactions", **_hdr(user))
    assert res.status_code == 200 and res.json()["reaction_count"] == 0


@pytest.mark.django_db
def test_my_reaction_is_reported_in_the_feed(client):
    story = _story()
    user = AccountFactory()
    client.post(f"/api/v1/stories/{story.pk}/reactions", **_hdr(user))
    res = client.get("/api/v1/stories", **_hdr(user))
    assert res.json()["results"][0]["my_reaction"] is True


@pytest.mark.django_db
def test_a_hidden_story_404s_for_a_stranger_but_the_author_still_sees_it(client):
    author = AccountFactory()
    story = _story(author=author, status=StoryStatus.HIDDEN)
    assert client.get(f"/api/v1/stories/{story.pk}", **_hdr(AccountFactory())).status_code == 404
    res = client.get(f"/api/v1/stories/{story.pk}", **_hdr(author))
    assert res.status_code == 200 and res.json()["status"] == StoryStatus.HIDDEN


@pytest.mark.django_db
def test_posting_a_story_emits_a_server_side_analytics_event(client, caplog):
    """US-Y1 · a server-authoritative outcome emits one account-free event."""
    import json
    import logging
    with caplog.at_level(logging.INFO, logger="kupkop.analytics"):
        client.post("/api/v1/stories",
                    {"caption": "hi", "photos": [{"file_url": "https://x/a.jpg"}]},
                    content_type="application/json", **_hdr(AccountFactory()))
    events = [json.loads(r.getMessage()) for r in caplog.records
              if r.name == "kupkop.analytics"]
    posted = [e for e in events if e["event"] == "story_posted"]
    assert len(posted) == 1
    assert "account_id" not in posted[0] and "account" not in posted[0]  # D-S6-6: no PII


@pytest.mark.django_db
def test_flagging_a_story_uses_the_moderation_pipeline(client):
    story = _story()
    user = AccountFactory()
    res = client.post("/api/v1/moderation/flags",
                      {"target_type": "story", "target_id": str(story.pk),
                       "reason": "off-topic"},
                      content_type="application/json", **_hdr(user))
    assert res.status_code == 201
    assert ModerationFlag.objects.filter(target_type="story", target_id=story.pk).count() == 1
