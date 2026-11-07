"""Review step 7f · a filter on what people post (Apple 1.2: "a method for filtering objectionable material from being
posted"; MQ11 option A, a server-side blocklist, with human review of reports behind it).

⚠️ WHAT THIS IS, SAID HONESTLY. It refuses a post that contains a term on the list. It does not read context, catch a
scam or spot a photo, and a determined person will get round it. It is the first of the two layers the stores ask for;
the second is a moderator acting on a report. The list is `moderation/blocklist.txt` plus `settings.MODERATION_BLOCKLIST_EXTRA`.

The refusal never says which word, so the list cannot be mined by trial, and it never keeps the text.

Rescue reports are exempt: someone describing an injured animal in the words they have must be able to send it (MQ1's
"an animal in trouble outranks it", the same exemption blocks have). Reports of a problem are exempt too, because they
quote the thing being reported.
"""
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from rest_framework import serializers

logger = logging.getLogger("kupkop.moderation")

BLOCKLIST_FILE = Path(__file__).with_name("blocklist.txt")
MESSAGE = "That contains language that isn't allowed on Kupkop. Please reword it and try again."

_SYMBOLS = str.maketrans({"@": "a", "0": "o", "1": "i", "3": "e", "$": "s", "5": "s", "7": "t"})
_WORD = re.compile(r"[a-z0-9@$]+")
_STRETCH = re.compile(r"(.)\1{2,}")


def _fold(text):
    """Lower-case, accents off, a letter stretched to three or more shrunk to one."""
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return _STRETCH.sub(r"\1", plain)


def _plain(word, ending):
    """"whores" is "whore" and "bitches" is "bitch": a plural is the same word. Only on a word long enough to be one."""
    return word[:-len(ending)] if len(word) > 4 and word.endswith(ending) and not word.endswith("ss") else word


def _words(text):
    """The words of `text` in every reading that matters: as written, with a plural taken off (-s, then -es), and each of
    those with the usual symbol-for-letter swaps undone. A list of word lists, one per reading."""
    found = _WORD.findall(_fold(text))
    readings = [found, [_plain(w, "s") for w in found], [_plain(w, "es") for w in found]]
    return readings + [[w.translate(_SYMBOLS) for w in r] for r in readings]


@lru_cache(maxsize=1)
def _terms():
    """({single words}, {phrases as tuples}), from the file and the setting. Read once per process."""
    lines = [ln.strip() for ln in BLOCKLIST_FILE.read_text(encoding="utf-8").splitlines()]
    lines += [str(t).strip() for t in getattr(settings, "MODERATION_BLOCKLIST_EXTRA", [])]
    singles, phrases = set(), set()
    for line in lines:
        if not line or line.startswith("#"):
            continue
        words = _words(line)[0]
        if len(words) == 1:
            singles.add(words[0])
        elif words:
            phrases.add(tuple(words))
    return singles, phrases


def blocked(text):
    """True when `text` contains a listed term as a whole word or phrase."""
    if not text:
        return False
    singles, phrases = _terms()
    for words in _words(text):
        if singles.intersection(words):
            return True
        for phrase in phrases:
            n = len(phrase)
            if any(tuple(words[i:i + n]) == phrase for i in range(len(words) - n + 1)):
                return True
    return False


class ModeratedCharField(serializers.CharField):
    """A `CharField` that refuses text on the blocklist, with one plain message. Use it for anything one person posts
    for another to read; leave rescue reports and reports-of-a-problem on the plain field."""
    default_error_messages = {"content_not_allowed": MESSAGE}

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if blocked(value):
            logger.info("refused a post on the blocklist (field=%s)", self.field_name)  # never the text
            self.fail("content_not_allowed")
        return value
