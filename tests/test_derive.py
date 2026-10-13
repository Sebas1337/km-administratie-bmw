"""Derivation edge cases seen on real days (ONTWERP.md §7.1)."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from kmlog.classify import classify_legs
from kmlog.derive import AMSTERDAM, PASS_SHORT, derive_episodes, derive_legs
from kmlog.model import Observation
from test_voorbeelddagen import CLIENT, HOME, LOCATIONS, ROUTES, UNKNOWN

DAY = date(2026, 10, 12)  # first client day with the live poller
NEAR_HOME = (HOME.lat + 0.0017, HOME.lon)  # ~190 m, inside the home radius
TWO_KM_FROM_HOME = (HOME.lat + 0.018, HOME.lon)


def at(hhmm_ss: str) -> datetime:
    h, m, *s = map(int, hhmm_ss.split(":"))
    return datetime(DAY.year, DAY.month, DAY.day, h, m, s[0] if s else 0, tzinfo=AMSTERDAM)


def obs(poll: str, odo: int, reported: str, pos, reported_day_before=False):
    lat, lon = pos
    ts = at(reported) - timedelta(days=1 if reported_day_before else 0)
    return Observation("car", at(poll), odo, ts, lat, lon, ts)


def first_client_day(evening: Observation) -> list[Observation]:
    """The readings of 2026-10-12, positions replaced by the test locations."""
    home, client = (HOME.lat, HOME.lon), (CLIENT.lat, CLIENT.lon)
    return [
        obs("06:00:27", 23198, "20:53:50", home, reported_day_before=True),
        obs("08:00:27", 23198, "08:00:10", home),
        obs("09:00:27", 23201, "08:59:54", NEAR_HOME),  # supermarket ride, leaving again
        obs("10:00:27", 23270, "09:54:22", client),
        *[obs(f"{h}:00:27", 23270, "09:54:22", client) for h in range(11, 17)],
        obs("17:00:27", 23270, "17:00:05", client),
        evening,
        obs("19:00:27", 23340, "18:04:56", home),
        obs("20:00:27", 23340, "18:04:56", home),
    ]


def derive(observations):
    episodes = derive_episodes(observations, LOCATIONS)
    legs = derive_legs(episodes, LOCATIONS)
    return legs, classify_legs(legs, LOCATIONS, ROUTES)


def test_reading_while_driving_does_not_split_the_trip_home():
    legs, segments = derive(first_client_day(obs("18:00:27", 23338, "17:58:17", TWO_KM_FROM_HOME)))
    assert [(leg.from_location, leg.to_location, leg.distance_km) for leg in legs] == [
        ("home", "home", 3),  # supermarket, private
        ("home", "client", 69),
        ("client", "home", 70),
    ]
    assert [leg.business_km for leg in legs] == [0, 69, 70]
    assert all(seg.question is None for seg in segments)
    (wp,) = legs[2].waypoints
    assert (wp.odometer_km, wp.reason, wp.seen_at) == (23338, PASS_SHORT, at("17:58:17"))
    assert legs[2].id == "car#20261012T1700"  # same id as the first half before the fix


def test_stop_of_more_than_ten_minutes_stays_a_stop():
    evening = obs("18:00:27", 23338, "17:45:00", TWO_KM_FROM_HOME)
    legs, segments = derive(first_client_day(evening))
    assert [leg.to_location for leg in legs[-2:]] == [None, "home"]
    assert segments[-1].question is not None
    assert legs[-1].waypoints == []


def test_garage_morning_is_one_private_trip():
    # 2026-10-13: charge cable plugged in at 08:47 (fresh report, same km), to the garage just
    # before 10:00, the garage drove it to an inspection and back around 11:00, home at 12:13.
    # The 40-minute stop at the garage was overwritten before the 11:00 poll saw it.
    home = (HOME.lat, HOME.lon)
    garage, inspection = (HOME.lat + 0.015, HOME.lon - 0.036), (HOME.lat - 0.007, HOME.lon - 0.033)
    legs, segments = derive(
        [
            obs("08:00:27", 23340, "20:00:00", home, reported_day_before=True),
            obs("09:00:27", 23340, "08:47:00", home),
            obs("10:00:27", 23342, "09:59:18", TWO_KM_FROM_HOME),
            obs("11:00:27", 23348, "10:57:49", inspection),
            obs("12:00:27", 23352, "11:59:18", garage),
            obs("13:00:27", 23356, "12:13:40", home),
            obs("14:00:27", 23356, "12:13:40", home),
        ]
    )
    (leg,) = legs
    assert (leg.from_location, leg.to_location, leg.distance_km) == ("home", "home", 16)
    assert (leg.business_km, leg.private_km) == (0, 16)
    assert [(wp.odometer_km, wp.reason) for wp in leg.waypoints] == [
        (23342, PASS_SHORT),
        (23348, PASS_SHORT),
        (23352, PASS_SHORT),
    ]
    assert all(seg.question is None for seg in segments)


def test_reading_at_a_known_location_is_always_a_stop():
    # 09:00 was taken while leaving, 190 m from home: still a home stop, so the private
    # supermarket ride stays separate from the commute
    legs, _ = derive(first_client_day(obs("18:00:27", 23338, "17:58:17", TWO_KM_FROM_HOME)))
    assert (legs[0].distance_km, legs[0].business_km) == (Decimal(3), Decimal(0))


def test_trailing_pass_through_waits_for_the_next_stop():
    readings = first_client_day(obs("18:00:27", 23338, "17:58:17", TWO_KM_FROM_HOME))[:-2]
    legs, _ = derive(readings)
    assert [leg.to_location for leg in legs] == ["home", "client"]  # no leg to the road yet


def test_unknown_stop_seen_for_hours_is_a_stop():
    readings = [
        obs("07:00:00", 100, "06:00:00", (HOME.lat, HOME.lon)),
        obs("09:00:00", 130, "08:30:00", UNKNOWN),
        obs("10:00:00", 130, "08:30:00", UNKNOWN),
        obs("12:00:00", 160, "11:30:00", (HOME.lat, HOME.lon)),
    ]
    legs, _ = derive(readings)
    assert [leg.to_location for leg in legs] == [None, "home"]
