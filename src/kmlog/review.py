"""Month review lists: trips that count as private but might have been business (ONTWERP §9).

- Unknown places: visits on workdays to a place that is not a location, with at least
  MIN_VISIT_KM for the segment (there and back). Visits within GROUP_RADIUS_M of each other are
  one place. One-off private places need no action: they already count as private.
- Weekend trips to clients: segments at a client/business place on a weekend or holiday that
  still count as private (rule 6), per place.

Both lists are informational; nothing here changes a leg.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from .classify import WORK_TYPES, Reason, is_workday
from .derive import distance_m
from .model import Leg, LegSource, Location

MIN_VISIT_KM = 10
GROUP_RADIUS_M = 200


@dataclass(frozen=True)
class Visit:
    date: date
    km: int  # the whole segment, e.g. home -> place -> home
    arrived_at: datetime | None
    departed_after: datetime | None  # last poll that saw the car still at the place


@dataclass
class UnknownPlace:
    latlon: tuple[float, float]
    visits: list[Visit] = field(default_factory=list)


@dataclass
class WeekendTrips:
    location_id: str
    name: str
    dates: list[date] = field(default_factory=list)


def _segment_km(legs: list[Leg]) -> dict[str, Decimal]:
    km: dict[str, Decimal] = {}
    for leg in legs:
        key = leg.segment_id or leg.id
        km[key] = km.get(key, Decimal(0)) + leg.distance_km
    return km


def unknown_places(legs: Iterable[Leg]) -> list[UnknownPlace]:
    cardata = sorted(
        (leg for leg in legs if leg.source is LegSource.CARDATA),
        key=lambda leg: (leg.date, leg.id),
    )
    seg_km = _segment_km(cardata)
    places: list[UnknownPlace] = []
    for i, leg in enumerate(cardata):
        if leg.to_location is not None or leg.to_latlon is None or not is_workday(leg.date):
            continue
        km = seg_km[leg.segment_id or leg.id]
        if km < MIN_VISIT_KM:
            continue
        nxt = cardata[i + 1] if i + 1 < len(cardata) else None
        visit = Visit(leg.date, int(km), leg.arrived_at, nxt.departed_after if nxt else None)
        place = next(
            (p for p in places if distance_m(*p.latlon, *leg.to_latlon) <= GROUP_RADIUS_M), None
        )
        if place is None:
            place = UnknownPlace(leg.to_latlon)
            places.append(place)
        place.visits.append(visit)
    return places


def weekend_client_trips(legs: Iterable[Leg], locations: Iterable[Location]) -> list[WeekendTrips]:
    work = {loc.id: loc for loc in locations if loc.type in WORK_TYPES}
    found: dict[str, WeekendTrips] = {}
    for leg in sorted(legs, key=lambda leg: (leg.date, leg.id)):
        if leg.reason != Reason.WEEKEND or leg.business_km:
            continue
        for loc_id in (leg.to_location, leg.from_location):
            if loc_id in work:
                trips = found.setdefault(loc_id, WeekendTrips(loc_id, work[loc_id].name))
                if leg.date not in trips.dates:
                    trips.dates.append(leg.date)
                break
    return sorted(found.values(), key=lambda t: t.name)
