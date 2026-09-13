"""The city-name spelling rule (common/cities.py) — pure, no database.

⚠️ WHAT THESE PIN. Not "a regex works", but the three real spellings that were in conflict
on 2026-09-09: the mobile picker's "Marikina City", the centroid table's "Marikina", and the
free-text listing rows. An account set to "Marikina City" saw an empty adoption feed and an
empty rescue map while seven of each sat in the database.
"""
from common.cities import canonical_city, city_variants
from sagip.geo import centroid_for


class TestCanonicalCity:
    def test_picker_and_listing_spellings_agree(self):
        # THE bug: these two are the same city and did not compare equal.
        assert canonical_city("Marikina City") == canonical_city("Marikina")
        assert canonical_city("Pasig City") == canonical_city("Pasig")

    def test_a_city_whose_name_keeps_city_still_matches_itself(self):
        # "Quezon City" is stored WITH the suffix; it must not stop matching now that the
        # suffix is stripped — both sides get stripped, so they still meet.
        assert canonical_city("Quezon City") == canonical_city("quezon city")
        assert canonical_city("Quezon City") == "quezon"

    def test_case_and_whitespace_are_not_significant(self):
        assert canonical_city("  marikina   CITY ") == "marikina"

    def test_distinct_cities_still_differ(self):
        # The normalisation must not collapse everything into one bucket.
        assert canonical_city("Manila") != canonical_city("Marikina")
        assert canonical_city("Makati") != canonical_city("Malabon")

    def test_blank_never_matches_a_real_city(self):
        for blank in (None, "", "   "):
            assert canonical_city(blank) == ""
            assert canonical_city(blank) != canonical_city("Manila")

    def test_city_is_only_stripped_at_the_end(self):
        # A plain replace() would maul this one.
        assert canonical_city("City of San Fernando") == "city of san fernando"


class TestCityVariants:
    def test_offers_both_spellings_for_a_picker_value(self):
        assert set(city_variants("Marikina City")) == {"Marikina City", "Marikina"}

    def test_offers_both_spellings_for_a_bare_value(self):
        assert set(city_variants("Marikina")) == {"Marikina City", "Marikina"}

    def test_blank_means_do_not_filter(self):
        assert city_variants(None) == []
        assert city_variants("   ") == []

    def test_no_duplicates(self):
        variants = city_variants("Manila")
        assert len(variants) == len(set(variants))


class TestCentroidLookup:
    def test_picker_spelling_now_resolves(self):
        # Both of these were None, and StrayReportMapView reads None as "no known city"
        # and returns zero pins — which is how a Marikina account got an empty map.
        assert centroid_for("Marikina City") == centroid_for("Marikina")
        assert centroid_for("Marikina City") is not None
        assert centroid_for("Pasig City") is not None

    def test_suffixed_key_still_resolves(self):
        assert centroid_for("Quezon City") is not None

    def test_unknown_city_is_still_unknown(self):
        # The tolerance must not turn an unknown name into a wrong pin.
        assert centroid_for("Cebu City") is None
        assert centroid_for("Nowhere") is None
        assert centroid_for(None) is None
