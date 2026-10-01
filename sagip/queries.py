"""Shared Sagip read queries — one definition per question, so two surfaces asking the same
thing can't drift into two answers.

`reports_near_city` is what "near <city>" means everywhere: the rescue map's list and the shelter
dashboard's Rescue card (S16) both read it, so the count on the card is the list the card opens.
"""
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D

from sagip.geo import centroid_for
from sagip.models import StrayReport

DEFAULT_RADIUS_KM = 10.0


def reports_near_city(city, radius_km=DEFAULT_RADIUS_KM):
    """Reports within `radius_km` of `city`'s centre, nearest first — or None when the city has
    no known centre (the map can't search it; S15). None is not "no reports": callers must say
    the city isn't covered, never show zero."""
    centroid_ll = centroid_for(city)
    if centroid_ll is None:
        return None
    lat, lng = centroid_ll
    centre = Point(lng, lat, srid=4326)
    return (StrayReport.objects
            .filter(geom__dwithin=(centre, D(km=radius_km)), hidden_at__isnull=True)
            .annotate(_distance=Distance("geom", centre))
            .order_by("_distance"))
