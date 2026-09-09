"""One spelling rule for city names, shared by every city-scoped query.

⚠️ WHY THIS EXISTS. Three places in this product name the same city three different ways,
and they agree only by accident:

    location picker (mobile)    "Marikina City"   "Pasig City"   "Quezon City"
    CITY_CENTROIDS (sagip.geo)  "Marikina"        "Pasig"        "Quezon City"
    adoption listings (data)    "Marikina"          —            "Quezon City"

The listing form is a free-text box, so the third row is whatever the poster typed. Exact
comparison therefore worked for Quezon City and Manila and failed silently for Marikina and
Pasig: the adoption feed filtered down to nothing, and the rescue map's `centroid_for()`
returned None, which that view reads as "no known city" and answers with zero pins.

Found 2026-09-09 — an account set to "Marikina City" was shown neither of the seven Marikina
listings nor any of the seven Marikina strays. It had been masked until then by a separate
client bug that never sent the city at all, so both feeds were accidentally nationwide.

⚠️ THIS DOES NOT FIX THE DRIFT. The real fix is a single canonical vocabulary shared by the
picker and the listing form, plus a migration of the rows already written. This only makes
the comparison tolerant enough that the feeds work in the meantime.
"""
import re

# Anchored to the END of the name only. A plain `replace(" city", "")` would also eat the
# middle of a name like "City of San Fernando" reversed into "San Fernando City Proper".
_CITY_SUFFIX = re.compile(r"\s+city$", re.IGNORECASE)


def canonical_city(value):
    """Compare-safe form of a city name: whitespace collapsed, trailing "City" dropped, folded.

    "Marikina City", "marikina  city" and "Marikina" all canonicalise to "marikina", so a
    picker value and a free-text listing value match in either direction. Returns "" for a
    missing or blank name — which is never equal to a real city's canonical form.
    """
    if not value:
        return ""
    collapsed = " ".join(str(value).split())
    return _CITY_SUFFIX.sub("", collapsed).casefold()


def city_variants(value):
    """The spellings a stored city plausibly uses for `value`, for an `iexact` OR-filter.

    Returned as an explicit short list rather than normalising inside SQL so the filter stays
    an ordinary indexed comparison: "Marikina City" -> ["Marikina City", "Marikina"], which
    matches a row stored either way. Empty list for a blank name, meaning "do not filter".
    """
    base = " ".join(str(value or "").split())
    if not base:
        return []
    stripped = _CITY_SUFFIX.sub("", base).strip()
    if not stripped:
        return []
    # dict.fromkeys keeps order and drops the duplicate when value had no "City" suffix.
    return list(dict.fromkeys([base, stripped, "%s City" % stripped]))
