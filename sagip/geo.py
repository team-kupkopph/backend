"""City centroids for the rescue-map proximity query (US-S4), and the precise-location
coarsening rule (US-SEC1).

The map is city-scoped: a viewer chooses a city, and reports are ranked by distance from
that city's centre. `stray_report.city` (reverse-geocoded) is out of MVP scope, so the
centroid — not the stored column — is what anchors the query. Metro Manila first, matching
the launch scope. Coordinates are (lat, lng), approximate city centres.
"""
import math

from common.cities import canonical_city

EARTH_RADIUS_M = 6371000
COARSEN_CELL_SIZE_M = 500

CITY_CENTROIDS = {
    "Marikina": (14.6507, 121.1029),
    "Pasig": (14.5764, 121.0851),
    "Quezon City": (14.6760, 121.0437),
    "Manila": (14.5995, 120.9842),
    "Makati": (14.5547, 121.0244),
    "Taguig": (14.5176, 121.0509),
    "Pasay": (14.5378, 121.0014),
    "Mandaluyong": (14.5794, 121.0359),
    "San Juan": (14.6019, 121.0355),
    "Caloocan": (14.6577, 120.9842),
}


def centroid_for(city):
    """(lat, lng) for a known city; None if unknown or unset.

    Matching goes through `canonical_city` so the mobile picker's "Marikina City" resolves
    to the "Marikina" key here, and "Pasig City" to "Pasig". Before that, both returned None
    and StrayReportMapView read the None as "no known city" and answered with zero pins —
    see common/cities.py for why the two vocabularies disagree in the first place.
    """
    key = canonical_city(city)
    if not key:
        return None
    for name, latlng in CITY_CENTROIDS.items():
        if canonical_city(name) == key:
            return latlng
    return None


def coarsen_point(lat, lng, cell_size_m=COARSEN_CELL_SIZE_M):
    """US-SEC1 · snap (lat, lng) to the centroid of a deterministic ~cell_size_m grid cell.

    Deterministic, not jittered: the same point always coarsens to the same output. That
    matters here specifically — a per-request random offset would average out over
    repeated calls, letting an unprivileged caller triangulate the real point just by
    asking enough times. A fixed grid has no such leak: every call for the same report
    returns the identical `approx_location`.

    The longitude-per-degree scale depends on latitude (`cos(lat)`); using each point's
    own latitude keeps the cell size close to `cell_size_m` at Metro Manila's ~14.6°N
    without needing a shared reference grid — the tiny non-uniformity this introduces
    between different reports' cells doesn't matter for a privacy coarsening, only for a
    true geodesic grid.
    """
    m_per_deg_lat = math.pi * EARTH_RADIUS_M / 180
    m_per_deg_lng = m_per_deg_lat * math.cos(math.radians(lat))
    lat_cell = math.floor(lat * m_per_deg_lat / cell_size_m)
    lng_cell = math.floor(lng * m_per_deg_lng / cell_size_m)
    centroid_lat = (lat_cell + 0.5) * cell_size_m / m_per_deg_lat
    centroid_lng = (lng_cell + 0.5) * cell_size_m / m_per_deg_lng
    return centroid_lat, centroid_lng
