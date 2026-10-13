from datetime import date
from decimal import Decimal

from kmlog.model import Classification, Leg, LegSource, LegStatus, Rate, Route


def test_rate_amount_in_cents():
    rate = Rate(year=2026, cents_per_km=25)
    assert rate.amount_cents(140) == 3500  # EUR 35.00


def test_route_connects_unordered_pair():
    route = Route(
        id="a4", location_a="home", location_b="client", name="A4", distance_km=70, is_default=True
    )
    assert route.connects("client", "home")
    assert not route.connects("home", "utrecht")


def test_leg_defaults_are_conservative():
    leg = Leg(
        id="2026-10-10#1",
        vehicle="car",
        date=date(2026, 10, 10),
        distance_km=Decimal(70),
    )
    assert leg.classification is Classification.PRIVATE
    assert leg.business_km == 0
    assert leg.status is LegStatus.AUTO
    assert leg.source is LegSource.CARDATA
