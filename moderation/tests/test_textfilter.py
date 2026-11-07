"""Review step 7f (Apple 1.2: a method for filtering objectionable material from being posted).

What these hold the filter to: it refuses a listed term as a WHOLE word or phrase however it is dressed up (case, accents,
symbol swaps, stretched letters), it never matches inside an innocent word, it applies on every field one person posts for
another to read, it leaves rescue reports and reports-of-a-problem alone, and it never says which word."""
import pytest
from django.test import override_settings

from community.models import StoryPost
from listings.models import AdoptionInquiry
from listings.tests.adoption_helpers import c, listing, person
from moderation import textfilter
from sagip.models import StrayReport
from volunteer.tests.helpers import verified_shelter


def _message(res):
    return res.json()["error"]["message"]


# ---- the matcher ------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "you are a bitch", "BITCH", "Bítch", "b1tch", "bi7ch", "biiiitch", "what a bitch!", "bitch.", "(bitch)",
    "whores", "Bitches", "putang ina mo", "PUTANG   INA", "putang-ina", "tangina", "t@ngina", "Tang ina mo",
])
def test_these_are_refused(text):
    assert textfilter.blocked(text)


@pytest.mark.parametrize("text", [
    "", "A friendly dog needs a home", "Bantay is a good boy", "classic", "assist the shelter", "Scunthorpe",
    "bitchin' deal on kibble is fine?",           # not on the list as a word: only the exact word is
    "I have 100 dogs and 3 cats", "analysis of the pass", "glass and brass",
])
def test_these_are_not(text):
    # "bitchin'" tokenises to "bitchin", which is not the listed word. A near-miss is not a match.
    assert not textfilter.blocked(text)


def test_a_phrase_matches_only_as_a_run_of_whole_words():
    assert textfilter.blocked("putang ina") and not textfilter.blocked("putang is not ina") and not textfilter.blocked("putang")


@override_settings(MODERATION_BLOCKLIST_EXTRA=["send deposit first", "wire transfer only"])
def test_the_owner_can_add_terms_without_a_deploy():
    textfilter._terms.cache_clear()
    try:
        assert textfilter.blocked("please SEND deposit first") and textfilter.blocked("Wire transfer only!")
        assert not textfilter.blocked("send the form first")
    finally:
        textfilter._terms.cache_clear()


def test_the_list_is_not_empty_and_has_no_blank_or_ambiguous_entries():
    """A filter that loads nothing refuses nothing, and every scan here has under-reported at least once."""
    singles, phrases = textfilter._terms()
    assert len(singles) >= 10 and len(phrases) >= 2
    assert all(len(w) >= 4 for w in singles), "a short term would match innocent words"


# ---- where it applies ----------------------------------------------------------------------------------
@pytest.mark.django_db
def test_an_inquiry_message_and_the_quick_answers_are_filtered():
    poster, adopter = person(), person()
    the_listing = listing(poster)
    url = f"/api/v1/listings/{the_listing.pk}/inquiries"
    for body in ({"message": "you bitch"}, {"application": {"experience": "fuck all"}}):
        res = c(adopter).post(url, body, format="json")
        assert res.status_code == 400 and _message(res) == textfilter.MESSAGE
    assert AdoptionInquiry.objects.count() == 0
    assert c(adopter).post(url, {"message": "I would love to meet Bantay"}, format="json").status_code == 201


@pytest.mark.django_db
def test_an_inquiry_message_has_a_length_limit():
    the_listing = listing(person())
    url = f"/api/v1/listings/{the_listing.pk}/inquiries"
    assert c(person()).post(url, {"message": "a" * 1001}, format="json").status_code == 400
    assert c(person()).post(url, {"message": "a" * 1000}, format="json").status_code == 201


@pytest.mark.django_db
def test_a_story_caption_is_filtered():
    author = person()
    res = c(author).post("/api/v1/stories", {"caption": "what a fucker", "story_type": "general"}, format="json")
    assert res.status_code == 400 and _message(res) == textfilter.MESSAGE and StoryPost.objects.count() == 0


@pytest.mark.django_db
def test_a_listing_name_and_story_are_filtered():
    poster = person()
    listing(poster)                                       # makes the poster a Verified Member
    body = {"pet": {"name": "Bantay", "species": "dog"}, "city": "Marikina", "adoption_fee": "0"}
    for patch in ({"description": "slut"}, {"requirements": "no whores"}, {"pet": {"name": "Bitch", "species": "dog"}}):
        res = c(poster).post("/api/v1/listings", {**body, **patch}, format="json")
        assert res.status_code == 400 and _message(res) == textfilter.MESSAGE, patch


@pytest.mark.django_db
def test_a_shift_title_and_description_are_filtered():
    shelter = verified_shelter()
    start = "2030-01-01T09:00:00+08:00"
    base = {"type": "walking", "starts_at": start, "ends_at": "2030-01-01T11:00:00+08:00", "capacity": 2}
    for patch in ({"title": "Walk the bitch"}, {"title": "Walk", "description": "fucking great"}):
        res = c(shelter).post("/api/v1/shelter/shifts", {**base, **patch}, format="json")
        assert res.status_code == 400 and _message(res) == textfilter.MESSAGE, patch


@pytest.mark.django_db
def test_a_rescue_report_and_a_report_of_a_problem_are_not_filtered():
    """An animal in trouble outranks the filter, and a report quotes what it reports."""
    reporter, poster = person(), person()
    res = c(reporter).post("/api/v1/moderation/flags", {"target_type": "listing", "target_id": str(listing(poster).pk),
                           "reason_code": "harassment", "reason": "they called me a bitch"}, format="json")
    assert res.status_code == 201
    created = c(reporter).post("/api/v1/reports", {"lat": 14.6507, "lng": 121.1029, "city": "Marikina", "report_type": "stray",
                                                   "species": "dog", "condition": "injured", "notes": "the fucking dog is bleeding, please come"},
                               format="json")
    assert created.status_code == 201 and StrayReport.objects.count() == 1


@pytest.mark.django_db
def test_the_refusal_never_names_the_word_or_keeps_the_text(caplog):
    caplog.set_level("INFO", logger="kupkop.moderation")
    res = c(person()).post("/api/v1/stories", {"caption": "total cunt", "story_type": "general"}, format="json")
    assert "cunt" not in res.content.decode().lower() and "cunt" not in caplog.text.lower()
    assert "refused a post on the blocklist" in caplog.text


@override_settings(MODERATION_BLOCKLIST_EXTRA=["bu", "glas", "dogg"])
def test_a_plural_is_only_taken_off_a_word_long_enough_to_be_one():
    """Without the guards, 'bus' would read as 'bu' and 'glass' as 'glas', and the owner's own list would refuse a bus and a
    glass. 'doggs' is a plural of a listed word and is still refused."""
    textfilter._terms.cache_clear()
    try:
        assert not textfilter.blocked("take the bus") and not textfilter.blocked("a glass of water")
        assert textfilter.blocked("doggs")
    finally:
        textfilter._terms.cache_clear()
