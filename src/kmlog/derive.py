"""Derivation: observations -> parking episodes -> legs. See ONTWERP.md §7.1.

Rules:
- A ParkingEpisode is a run of consecutive observations with the same odometer value.
  `first_seen` is the odometer_ts of the first observation (when the car reported the stop);
  `last_seen` is the polled_at of the last one (the last moment we know it was still parked —
  odometer_ts stays frozen while parked, so it cannot tell us that).
- A pass-through point is an episode at an unknown place that is not a proven stop: it stood
  there for less than MIN_STOP (one poll whose odometer_ts is
  only minutes old: the car was awake, i.e. driving). The i4 also reports while driving
  (2026-10-12: 17:58 reading 2 km before home), and such a reading must not split the trip.
  Pass-through points are dropped from the chain and kept on the leg as waypoints. Episodes at
  a known location always stay stops (a home stop separates a private ride from the commute).
- A Leg spans two consecutive stops; distance = odometer delta.
- A leg's day is the Amsterdam calendar day on which the car left home for the trip the leg
  belongs to (a trip = legs from one home departure until the next), so a night shift that
  returns the next morning counts once, on the departure day (§7.3 day F). Legs not preceded
  by a home departure, or more than a day after it, fall back to their own departure day.
- A leg across a poll gap (MIN_GAP_POLLS or more polls inside the poll window without an
  observation between departure and arrival, e.g. BMW down for a weekend) may hide several
  trips. It gets FLAG_POLL_GAP and the arrival day: the arrival is the only moment we know.
  Its id is based on the arrival too, so the date in the id stays near the leg's date.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .model import (
    Leg,
    Location,
    LocationType,
    Observation,
    ParkingEpisode,
    Settings,
    Waypoint,
)

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
EARTH_RADIUS_M = 6_371_000
MIN_STOP = timedelta(minutes=10)
PASS_SHORT = "short"  # stood still for less than MIN_STOP as far as we know
MIN_GAP_POLLS = 3  # missed polls between departure and arrival that make a poll gap
POLL_SLACK = timedelta(minutes=30)  # an observation this close to the hour counts for that poll
FLAG_POLL_GAP = "poll_gap"


def amsterdam_date(ts: datetime) -> date:
    return ts.astimezone(AMSTERDAM).date()


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def match_location(
    lat: float | None, lon: float | None, locations: Iterable[Location], on: date | None = None
) -> Location | None:
    """Nearest location whose radius contains (lat, lon) and that is active on `on`."""
    if lat is None or lon is None:
        return None
    best: tuple[float, Location] | None = None
    for loc in locations:
        if on is not None and (
            (loc.active_from and on < loc.active_from) or (loc.active_to and on > loc.active_to)
        ):
            continue
        d = distance_m(lat, lon, loc.lat, loc.lon)
        if d <= loc.radius_m and (best is None or d < best[0]):
            best = (d, loc)
    return best[1] if best else None


def derive_episodes(
    observations: Iterable[Observation], locations: Iterable[Location] = ()
) -> list[ParkingEpisode]:
    locations = list(locations)
    obs = sorted(observations, key=lambda o: o.polled_at)
    episodes: list[ParkingEpisode] = []
    for o in obs:
        current = episodes[-1] if episodes else None
        if current is not None and current.odometer_km == o.odometer_km:
            current.last_seen = max(current.last_seen, o.polled_at)
            if current.lat is None and o.lat is not None:
                current.lat, current.lon = o.lat, o.lon
            continue
        if current is not None and o.odometer_km < current.odometer_km:
            raise ValueError(
                f"odometer went backwards: {current.odometer_km} -> {o.odometer_km} "
                f"at {o.polled_at}"
            )
        episodes.append(
            ParkingEpisode(
                vehicle=o.vehicle,
                odometer_km=o.odometer_km,
                lat=o.lat,
                lon=o.lon,
                first_seen=o.odometer_ts,
                last_seen=o.polled_at,
            )
        )
    for ep in episodes:
        loc = match_location(ep.lat, ep.lon, locations, amsterdam_date(ep.first_seen))
        ep.location_id = loc.id if loc else None
        ep.pass_through = pass_through_reason(ep)
    return episodes


def pass_through_reason(ep: ParkingEpisode) -> str | None:
    """Why `ep` is a reading on the way rather than a stop, or None if it is a stop."""
    if ep.location_id is not None:
        return None
    if ep.last_seen - ep.first_seen < MIN_STOP:
        return PASS_SHORT
    return None


def missed_polls(
    polled: Sequence[datetime], start: datetime, end: datetime, settings: Settings
) -> list[datetime]:
    """Polls the poller should have made strictly between `start` and `end` (on the hour, inside
    the poll window) without an observation within POLL_SLACK. `polled` must be sorted."""
    missed = []
    t = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while t < end:
        if settings.polls_at(t.astimezone(AMSTERDAM).hour):
            i = bisect_left(polled, t - POLL_SLACK)
            if i == len(polled) or polled[i] > t + POLL_SLACK:
                missed.append(t)
        t += timedelta(hours=1)
    return missed


def gap_days(leg: Leg) -> list[date]:
    """Amsterdam days a leg across a poll gap may have driven on: departure day .. leg day."""
    first = amsterdam_date(leg.departed_after) if leg.departed_after else leg.date
    return [first + timedelta(days=i) for i in range((leg.date - first).days + 1)]


def derive_legs(
    episodes: list[ParkingEpisode],
    locations: Iterable[Location] = (),
    polled: Sequence[datetime] | None = None,
    settings: Settings | None = None,
) -> list[Leg]:
    """Legs between consecutive stops. Pass-through points become waypoints of the leg they
    lie on; leading ones are skipped and trailing ones (the car is on its way now) wait for
    the next stop. With `polled` (sorted polled_at of the observations), legs across a poll gap
    are flagged and dated on their arrival."""
    home_ids = {loc.id for loc in locations if loc.type is LocationType.HOME}
    stops: list[ParkingEpisode] = []
    between: dict[int, list[Waypoint]] = {}  # index in stops -> waypoints after that stop
    for ep in episodes:
        if ep.pass_through is None:
            stops.append(ep)
        elif stops:
            between.setdefault(len(stops) - 1, []).append(
                Waypoint(ep.first_seen, ep.odometer_km, ep.lat, ep.lon, ep.pass_through)
            )
    legs: list[Leg] = []
    trip_date: date | None = None
    for i, (ep1, ep2) in enumerate(zip(stops, stops[1:], strict=False)):
        gap = polled is not None and (
            len(missed_polls(polled, ep1.last_seen, ep2.first_seen, settings or Settings()))
            >= MIN_GAP_POLLS
        )
        own_date = amsterdam_date(ep1.last_seen)
        leg_id = f"{ep1.vehicle}#{ep1.last_seen.astimezone(AMSTERDAM):%Y%m%dT%H%M}"
        if gap:
            trip_date = amsterdam_date(ep2.first_seen)
            leg_id = f"{ep1.vehicle}#{ep2.first_seen.astimezone(AMSTERDAM):%Y%m%dT%H%M}-gap"
        # a trip spans at most one night; anything longer starts a new day on its own
        elif ep1.location_id in home_ids or trip_date is None or (own_date - trip_date).days > 1:
            trip_date = own_date
        legs.append(
            Leg(
                id=leg_id,
                vehicle=ep1.vehicle,
                date=trip_date,
                distance_km=Decimal(ep2.odometer_km - ep1.odometer_km),
                departed_after=ep1.last_seen,
                arrived_at=ep2.first_seen,
                from_location=ep1.location_id,
                to_location=ep2.location_id,
                from_latlon=(ep1.lat, ep1.lon) if ep1.lat is not None else None,
                to_latlon=(ep2.lat, ep2.lon) if ep2.lat is not None else None,
                odometer_from=ep1.odometer_km,
                odometer_to=ep2.odometer_km,
                waypoints=between.get(i, []),
                flags=[FLAG_POLL_GAP] if gap else [],
            )
        )
    return legs
