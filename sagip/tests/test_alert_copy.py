"""C9 (dev/test-plan-sagip.md §0.3) · "A injured dog near you needs help" — the alert and
escalation copy built `f"A {condition} {species}"`, wrong for the most urgent kind of report.
"""
from sagip.notices import with_article


def test_the_article_follows_the_word():
    assert with_article("injured dog") == "An injured dog"
    assert with_article("sick cat") == "A sick cat"
    assert with_article("healthy dog") == "A healthy dog"
    assert with_article("pregnant cat") == "A pregnant cat"
    assert with_article("other animal") == "An other animal"
