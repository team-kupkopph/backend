import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from community.badges import impact_counts
from sagip.models import RescueCase, StrayReport


@pytest.mark.django_db
def test_a_rescue_of_your_own_report_does_not_count():
    me = AccountFactory()
    mine = StrayReport.objects.create(reporter_account=me, species="dog", condition="injured",
                                      status="resolved", geom=Point(121.1, 14.65, srid=4326))
    theirs = StrayReport.objects.create(reporter_account=AccountFactory(), species="dog",
                                        condition="injured", status="resolved",
                                        geom=Point(121.1, 14.65, srid=4326))
    for r in (mine, theirs):
        RescueCase.objects.create(report=r, claimed_by_account=me, resolved_at=timezone.now())
    assert impact_counts(me)["rescues_helped"] == 1
