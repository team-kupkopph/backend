"""US-E3 · the smoke suite's own assertions.

The suite runs against a deployed URL, so its checks cannot be exercised in CI without a
deployment. Its *logic* can, and one piece of that logic is load-bearing: the detector
that decides whether a public payload leaked a precise coordinate. A detector that
answers "clean" because it did not look deep enough is the worst possible outcome — it
would sign off the exact §12.5 breach it exists to catch.
"""
import pytest
from smoke import find_precise_coordinates


def test_a_bare_lat_lng_pair_is_found():
    assert find_precise_coordinates({"lat": 14.6349, "lng": 121.0509})


def test_it_looks_inside_nested_lists_and_objects():
    # The rescue map returns {"reports": [ {...}, ... ]} — a top-level-only check would
    # pass every payload this suite exists to inspect.
    payload = {"reports": [{"report_id": "x"}, {"report_id": "y", "geom": {"lat": 1.0}}]}
    # The OUTERMOST hit, once: `geom` leaking means its whole subtree leaked, and
    # reporting geom, geom.lat and geom.lng separately triples the noise for one fact.
    assert find_precise_coordinates(payload) == ["reports[1].geom"]


def test_the_approximate_location_the_api_is_SUPPOSED_to_return_is_not_a_finding():
    # §12.5's coarsened pin is a deliberate, documented disclosure. Flagging it would make
    # the check cry wolf on every run, and a check that always fails gets switched off.
    assert find_precise_coordinates({"approx_location": {"lat": 14.6, "lng": 121.0}}) == []


def test_a_path_is_reported_not_just_a_boolean():
    # "Something leaked" is not actionable at 2am; "reports[3].geom.lat" is.
    found = find_precise_coordinates({"a": {"b": [{"longitude": 1}]}})
    assert found == ["a.b[0].longitude"]


@pytest.mark.parametrize("value", [None, [], {}, 42, "s", {"lat": None}])
def test_it_never_raises_on_odd_payloads(value):
    assert find_precise_coordinates(value) == []
