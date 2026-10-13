"""The example days from ONTWERP.md §7.3, end to end: hourly observations -> derive -> classify."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from kmlog.classify import (
    FLAG_WEEKEND_OR_HOLIDAY,
    Answer,
    Question,
    apply_answer,
    classify_legs,
    day_mismatch,
    dutch_holidays,
    easter,
    missed_business_days,
    time_out,
)
from kmlog.derive import AMSTERDAM, derive_episodes, derive_legs, distance_m
from kmlog.model import LegStatus, Location, LocationType, Observation, Route

HOME = Location("home", "Thuis", LocationType.HOME, 52.0000, 4.5000)
CLIENT = Location("client", "Klant", LocationType.CLIENT, 52.3000, 4.9000)
LOCATIONS = [HOME, CLIENT]
ROUTES = [
    Route("a4", "home", "client", "A4", 70, is_default=True),
    Route("ams", "home", "client", "via Amsterdam", 84),
    Route("n11", "home", "client", "N11", 77),
]
UNKNOWN = (51.8000, 5.2000)  # somewhere not on file
POS = {"home": (HOME.lat, HOME.lon), "client": (CLIENT.lat, CLIENT.lon), "unknown": UNKNOWN}

START_KM = 40_000


def observations(day: date, stops: list[tuple[str, str, str, int]]) -> list[Observation]:
    """Hourly polls for a list of (arrive HH:MM, leave HH:MM, place, odometer) stops.

    The car reports its odometer at arrival (odometer_ts = arrival); polls happen on the hour
    while parked. Times may exceed 24:00 to continue into the next day.
    """
    obs = []
    for arrive, leave, place, odo in stops:
        t_arrive = _at(day, arrive)
        t_leave = _at(day, leave)
        t = t_arrive.replace(minute=0) + timedelta(hours=1)
        while t <= t_leave:
            lat, lon = POS[place]
            obs.append(Observation("car", t, odo, t_arrive, lat, lon, t_arrive))
            t += timedelta(hours=1)
    return obs


def _at(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, tzinfo=AMSTERDAM) + timedelta(hours=h, minutes=m)


def run(day: date, stops):
    obs = observations(day, stops)
    episodes = derive_episodes(obs, LOCATIONS)
    legs = derive_legs(episodes, LOCATIONS)
    segments = classify_legs(legs, LOCATIONS, ROUTES)
    return legs, segments


def business(legs, on: date | None = None) -> Decimal:
    return sum((leg.business_km for leg in legs if on is None or leg.date == on), Decimal(0))


WORKDAY = date(2026, 10, 7)  # Wednesday


def normal_day(return_km: int):
    return [
        ("00:00", "07:30", "home", START_KM),
        ("08:30", "16:30", "client", START_KM + 71),
        ("17:45", "23:00", "home", START_KM + 71 + return_km),
    ]


def test_a_normal_day():
    legs, segments = run(WORKDAY, normal_day(70))
    assert [leg.distance_km for leg in legs] == [71, 70]
    assert business(legs) == 141
    assert all(leg.status is LegStatus.AUTO for leg in legs)
    assert [leg.matched_route for leg in legs] == ["a4", "a4"]
    assert all(leg.date == WORKDAY for leg in legs)
    assert [(leg.odometer_from, leg.odometer_to) for leg in legs] == [
        (START_KM, START_KM + 71),
        (START_KM + 71, START_KM + 141),
    ]


def test_b_detour_due_to_traffic():
    legs, segments = run(WORKDAY, normal_day(85))
    assert business(legs) == 156
    assert legs[1].matched_route == "ams"
    assert all(s.question is None for s in segments)


def _day_c():
    return run(
        WORKDAY,
        [
            ("00:00", "07:30", "home", START_KM),
            ("08:30", "16:30", "client", START_KM + 71),
            ("17:40", "21:10", "unknown", START_KM + 71 + 38),
            ("22:00", "23:00", "home", START_KM + 71 + 38 + 51),
        ],
    )


def test_c_long_private_detour_answer_private():
    legs, segments = _day_c()
    assert sum(leg.distance_km for leg in legs) == 160
    question = segments[-1]
    assert question.rule == 4
    assert question.question is Question.UNKNOWN_STOP
    assert question.unknown_stops == [UNKNOWN]
    assert business(legs) == 141  # conservative default before any answer
    apply_answer(question, Answer.PRIVATE)
    assert business(legs) == 141
    assert all(leg.status is LegStatus.CONFIRMED for leg in question.legs)


def test_c_prime_answer_business():
    legs, segments = _day_c()
    apply_answer(segments[-1], Answer.BUSINESS)
    assert business(legs) == 160


def test_d_short_invisible_stop():
    legs, _ = run(WORKDAY, normal_day(73))
    assert business(legs) == 144


def test_e_motor_day():
    obs = observations(WORKDAY, [("00:00", "23:00", "home", START_KM)])
    episodes = derive_episodes(obs, LOCATIONS)
    assert len(episodes) == 1
    assert derive_legs(episodes, LOCATIONS) == []


def test_f_night_shift_counts_on_departure_day():
    day = date(2026, 7, 2)
    legs, _ = run(
        day,
        [
            ("00:00", "20:10", "home", START_KM),
            ("21:10", "32:30", "client", START_KM + 70),  # until 08:30 on 3 jul
            ("33:30", "40:00", "home", START_KM + 140),
        ],
    )
    assert [leg.date for leg in legs] == [day, day]
    assert business(legs, day) == 140


def test_g_unknown_detour_without_visible_stop():
    legs, segments = run(WORKDAY, normal_day(96))
    seg = segments[-1]
    assert seg.rule == 2
    assert seg.question is Question.DETOUR_OR_PRIVATE_STOP
    assert legs[1].status is LegStatus.NEEDS_CONFIRMATION
    assert business(legs) == 141
    time_out(seg)
    assert business(legs) == 141
    assert legs[1].status is LegStatus.TIMED_OUT


def test_g_answer_detour():
    legs, segments = run(WORKDAY, normal_day(96))
    apply_answer(segments[-1], Answer.DETOUR)
    assert business(legs) == 167


def test_h_weekend_at_client():
    saturday = date(2026, 10, 10)
    legs, _ = run(saturday, normal_day(70))
    assert business(legs) == 141
    assert all(FLAG_WEEKEND_OR_HOLIDAY in leg.flags for leg in legs)
    assert all(leg.status is LegStatus.NEEDS_CONFIRMATION for leg in legs)


def test_i_private_day():
    legs, segments = run(
        WORKDAY,
        [
            ("00:00", "10:00", "home", START_KM),
            ("10:40", "14:00", "unknown", START_KM + 23),
            ("14:40", "23:00", "home", START_KM + 46),
        ],
    )
    assert business(legs) == 0
    assert len(segments) == 1
    assert segments[0].rule == 5
    assert segments[0].question is Question.BUSINESS_OR_PRIVATE
    assert missed_business_days(legs) == [WORKDAY]
    apply_answer(segments[0], Answer.BUSINESS)
    assert business(legs) == 46


def test_i_private_day_on_weekend_asks_nothing():
    legs, segments = run(
        date(2026, 10, 10),
        [
            ("00:00", "10:00", "home", START_KM),
            ("10:40", "14:00", "unknown", START_KM + 23),
            ("14:40", "23:00", "home", START_KM + 46),
        ],
    )
    assert segments[0].question is None
    assert missed_business_days(legs) == []


def test_j_missed_poll_flags_day():
    assert day_mismatch(day_delta_km=141, legs_km=Decimal(0))
    assert not day_mismatch(day_delta_km=141, legs_km=Decimal(141))
    assert not day_mismatch(day_delta_km=0, legs_km=Decimal(0))


# --- building blocks ----------------------------------------------------------------------


def test_episode_timestamps():
    obs = observations(WORKDAY, normal_day(70))
    episodes = derive_episodes(obs, LOCATIONS)
    assert [ep.location_id for ep in episodes] == ["home", "client", "home"]
    client = episodes[1]
    assert client.first_seen == _at(WORKDAY, "08:30")  # reported by the car on arrival
    assert client.last_seen == _at(WORKDAY, "16:00")  # last poll that still saw it parked


def test_odometer_backwards_is_an_error():
    t = _at(WORKDAY, "10:00")
    obs = [
        Observation("car", t, 100, t, None, None, None),
        Observation("car", t + timedelta(hours=1), 90, t, None, None, None),
    ]
    with pytest.raises(ValueError):
        derive_episodes(obs)


def test_haversine_sanity():
    # Amsterdam Centraal -> Utrecht Centraal is about 35 km as the crow flies
    assert 34_000 < distance_m(52.3791, 4.9003, 52.0894, 5.1100) < 37_000


def test_dutch_holidays_2026():
    assert easter(2026) == date(2026, 4, 5)
    holidays = dutch_holidays(2026)
    assert date(2026, 4, 27) in holidays  # Koningsdag
    assert date(2026, 5, 25) in holidays  # 2e Pinksterdag
    assert date(2026, 5, 14) in holidays  # Hemelvaart
    assert date(2026, 5, 5) not in holidays
