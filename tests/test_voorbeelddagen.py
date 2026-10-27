"""The example days from ONTWERP.md §7.3, end to end: hourly observations -> derive -> classify."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from kmlog.classify import (
    FLAG_IMPLAUSIBLE,
    FLAG_WEEKEND_OR_HOLIDAY,
    Answer,
    Distances,
    Question,
    Reason,
    apply_answer,
    classify_legs,
    dutch_holidays,
    easter,
    explain,
    margin,
    missed_business_days,
    time_out,
)
from kmlog.derive import AMSTERDAM, FLAG_POLL_GAP, derive_episodes, derive_legs, distance_m
from kmlog.model import Leg, LegStatus, Location, LocationType, Observation

# straight-line: home-client 50 km, home-client2 30.3 km; STOP is 40 km from home and 29.9 km
# from client (a 30-40-50 triangle), UNKNOWN is somewhere not on file
HOME = Location("home", "Thuis", LocationType.HOME, 52.0000, 4.5000)
CLIENT = Location("client", "Klant", LocationType.CLIENT, 52.4497, 4.5000)
CLIENT2 = Location("client2", "Klant 2", LocationType.CLIENT, 52.0000, 4.0574)
LOCATIONS = [HOME, CLIENT, CLIENT2]
STOP = (52.2878, 4.8517)
UNKNOWN = STOP
POS = {
    "home": (HOME.lat, HOME.lon),
    "client": (CLIENT.lat, CLIENT.lon),
    "client2": (CLIENT2.lat, CLIENT2.lon),
    "unknown": UNKNOWN,
}

START_KM = 40_000


def past_trips(kms: list[int], before: date) -> list[Leg]:
    """Earlier CarData trips home <-> client, one per day, oldest first."""
    legs = []
    for i, km in enumerate(kms):
        day = before - timedelta(days=len(kms) - i)
        a, b = ("home", "client") if i % 2 == 0 else ("client", "home")
        at = datetime(day.year, day.month, day.day, 8, tzinfo=AMSTERDAM)
        legs.append(
            Leg(
                f"car#{day:%Y%m%d}T0800",
                "car",
                day,
                Decimal(km),
                departed_after=at,
                arrived_at=at + timedelta(hours=1),
                from_location=a,
                to_location=b,
            )
        )
    return legs


WORKDAY = date(2026, 10, 7)  # Wednesday
HISTORY = past_trips([70, 69, 70, 84, 71, 70], before=date(2026, 7, 1))  # E = 70, fits <= 80


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


def run(day: date, stops, locations=LOCATIONS, history=HISTORY, manual_days=()):
    obs = observations(day, stops)
    episodes = derive_episodes(obs, locations)
    legs = derive_legs(episodes, locations, [o.polled_at for o in obs])
    segments = classify_legs(legs, locations, history, manual_days)
    return legs, segments


def business(legs, on: date | None = None) -> Decimal:
    return sum((leg.business_km for leg in legs if on is None or leg.date == on), Decimal(0))


def normal_day(return_km: int, place: str = "client", out_km: int = 71):
    return [
        ("00:00", "07:30", "home", START_KM),
        ("08:30", "16:30", place, START_KM + out_km),
        ("17:45", "23:00", "home", START_KM + out_km + return_km),
    ]


def test_fixture_distances():
    crow = {
        loc.id: round(distance_m(HOME.lat, HOME.lon, loc.lat, loc.lon) / 1000, 1)
        for loc in (CLIENT, CLIENT2)
    }
    assert crow == {"client": 50.0, "client2": 30.3}
    assert round(distance_m(*STOP, HOME.lat, HOME.lon) / 1000) == 40
    assert round(distance_m(*STOP, CLIENT.lat, CLIENT.lon) / 1000) == 30


def test_a_normal_day():
    legs, segments = run(WORKDAY, normal_day(70))
    assert [leg.distance_km for leg in legs] == [71, 70]
    assert business(legs) == 141
    assert all(leg.status is LegStatus.AUTO for leg in legs)
    assert [s.reason for s in segments] == [Reason.FITS, Reason.FITS]
    assert [leg.expected_km for leg in legs] == [70, 70]
    assert all(leg.date == WORKDAY for leg in legs)
    assert [(leg.odometer_from, leg.odometer_to) for leg in legs] == [
        (START_KM, START_KM + 71),
        (START_KM + 71, START_KM + 141),
    ]


def test_b_detour_due_to_traffic():
    legs, segments = run(WORKDAY, normal_day(85))
    seg = segments[-1]
    assert (seg.rule, seg.question) == (2, Question.DETOUR_OR_PRIVATE_STOP)
    assert business(legs) == 141
    apply_answer(seg, Answer.DETOUR)
    assert business(legs) == 156


def _day_c(locations=LOCATIONS):
    return run(
        WORKDAY,
        [
            ("00:00", "07:30", "home", START_KM),
            ("08:30", "16:30", "client", START_KM + 71),
            ("17:40", "21:10", "unknown", START_KM + 71 + 38),
            ("22:00", "23:00", "home", START_KM + 71 + 38 + 51),
        ],
        locations,
    )


def test_c_long_private_detour_with_visible_stop():
    legs, segments = _day_c()
    assert sum(leg.distance_km for leg in legs) == 160
    seg = segments[-1]
    assert (seg.rule, seg.reason, seg.question) == (3, Reason.STOP, None)
    assert business(legs) == 141
    assert all(leg.status is LegStatus.AUTO for leg in legs)


def test_c_prime_stop_saved_as_client():
    shop = Location("shop", "Klant 3", LocationType.CLIENT, *STOP)
    legs, segments = _day_c([*LOCATIONS, shop])
    assert [s.reason for s in segments] == [Reason.FITS, Reason.FIRST_TRIP, Reason.FIRST_TRIP]
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


def test_f_night_elsewhere_starts_a_new_day():
    # client -> somewhere for the night -> home the next morning: the trip home is on day 2
    day = date(2026, 10, 26)
    legs, segments = run(
        day,
        [
            ("00:00", "07:30", "home", START_KM),
            ("08:30", "16:00", "client", START_KM + 70),
            ("18:12", "32:00", "unknown", START_KM + 138),  # until 08:00 the next day
            ("32:58", "40:00", "home", START_KM + 152),
        ],
    )
    assert [leg.date for leg in legs] == [day, day, day + timedelta(days=1)]
    assert segments[1].reason is Reason.STOP  # still one segment client -> stop -> home
    assert [int(leg.business_km) for leg in legs] == [70, 68, 2]


def test_f_night_shift_with_a_stop_on_the_way_back():
    day = date(2026, 7, 2)
    legs, _ = run(
        day,
        [
            ("00:00", "20:10", "home", START_KM),
            ("21:10", "32:30", "client", START_KM + 70),  # until 08:30 on 3 jul
            ("33:00", "34:30", "unknown", START_KM + 80),  # bakery on the way home
            ("35:00", "40:00", "home", START_KM + 140),
        ],
    )
    assert [leg.date for leg in legs] == [day, day, day]


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
    legs, segments = run(saturday, normal_day(70, out_km=70))
    assert business(legs) == 0
    assert all(FLAG_WEEKEND_OR_HOLIDAY in leg.flags for leg in legs)
    assert all(leg.status is LegStatus.AUTO for leg in legs)  # no open question
    assert [s.question for s in segments] == [Question.WEEKEND] * 2
    assert explain(legs[0]) == "Privé: zaterdag. Toch zakelijk? Dan telt 70 km."
    for seg in segments:  # "Toch zakelijk"
        apply_answer(seg, Answer.CONFIRM)
    assert business(legs) == 140
    assert explain(legs[0]) == "Zaterdag, toch zakelijk."


def _private_day(day):
    return run(
        day,
        [
            ("00:00", "10:00", "home", START_KM),
            ("10:40", "14:00", "unknown", START_KM + 23),
            ("14:40", "23:00", "home", START_KM + 46),
        ],
    )


def test_i_private_day():
    legs, segments = _private_day(WORKDAY)
    assert business(legs) == 0
    assert len(segments) == 1
    assert (segments[0].rule, segments[0].question) == (5, None)
    assert all(leg.status is LegStatus.AUTO for leg in legs)
    assert explain(legs[0]) == "Privé: geen klant of zakelijke plek."
    assert missed_business_days(legs) == [WORKDAY]


def test_i_private_day_on_weekend():
    legs, segments = _private_day(date(2026, 10, 10))
    assert segments[0].question is None
    assert missed_business_days(legs) == []


FRIDAY = date(2026, 10, 9)
MONDAY = date(2026, 10, 12)


def weekend_gap(out_km: int = 93):
    """No polls from Friday 19:00 (BMW down) until Monday 10:00; meanwhile the car drove
    privately over the weekend and to the client on Monday morning (arrived 09:22)."""
    return [
        ("00:00", "19:00", "home", START_KM),
        ("81:22", "88:30", "client", START_KM + out_km),
        ("89:45", "95:00", "home", START_KM + out_km + 69),
    ]


def test_j_poll_gap_over_the_weekend():
    legs, segments = run(FRIDAY, weekend_gap())
    gap = legs[0]
    assert gap.flags == [FLAG_POLL_GAP]
    assert (gap.date, gap.id) == (MONDAY, "car#20261012T0922-gap")
    assert (segments[0].rule, segments[0].reason, segments[0].question) == (8, Reason.GAP, None)
    assert (gap.business_km, gap.private_km, gap.status) == (70, 23, LegStatus.AUTO)
    assert explain(gap) == (
        "Geen metingen tussen vr 09-10 19:00 en ma 12-10 09:22. "
        "Zakelijk telt de verwachte 70 km; wat de auto verder reed, is privé."
    )
    assert legs[1].flags == [] and legs[1].expected_km == 70
    assert business(legs, FRIDAY) == 0
    assert business(legs, MONDAY) == 139


def test_j_poll_gap_that_fits_counts_all_km():
    legs, segments = run(FRIDAY, weekend_gap(out_km=71))
    assert (segments[0].reason, legs[0].business_km) == (Reason.FITS, 71)
    assert explain(legs[0]).startswith("Geen metingen tussen vr 09-10 19:00 en ma 12-10 09:22. ")


def test_j_manual_trips_take_over_the_gap():
    legs, segments = run(FRIDAY, weekend_gap(), manual_days={MONDAY})
    assert (segments[0].rule, segments[0].reason) == (8, Reason.GAP_MANUAL)
    assert legs[0].business_km == 0
    assert explain(legs[0]).endswith("Privé: je hebt voor deze dagen zelf ritten toegevoegd.")
    assert business(legs) == 69  # the return trip; the user adds Monday morning by hand


def test_j_poll_gap_on_workdays_home_to_home():
    tuesday = date(2026, 10, 6)  # BMW down from Tuesday 08:00 until Thursday 19:00
    legs, segments = run(tuesday, [("00:00", "07:00", "home", 0), ("66:40", "70:00", "home", 420)])
    assert [(leg.date, leg.flags, leg.business_km) for leg in legs] == [
        (date(2026, 10, 8), [FLAG_POLL_GAP], 0)
    ]
    assert segments[0].reason == Reason.SAME_PLACE


def test_j_gap_without_driving_or_short_gap_while_driving():
    # no polls 09:00-13:00 at home: same odometer, so no leg at all
    legs, _ = run(WORKDAY, [("00:00", "08:00", "home", 0), ("13:30", "23:00", "home", 0)])
    assert legs == []
    # two missed polls while driving (last poll 07:00, arrived 09:50): an ordinary leg
    legs, _ = run(WORKDAY, [("00:00", "07:30", "home", 0), ("09:50", "23:00", "client", 70)])
    assert legs[0].flags == [] and legs[0].date == WORKDAY
    # three (08, 09 and 10:00, arrived 10:10): a gap
    legs, _ = run(WORKDAY, [("00:00", "07:30", "home", 0), ("10:10", "23:00", "client", 70)])
    assert legs[0].flags == [FLAG_POLL_GAP]


def test_j_gap_legs_do_not_teach_distances():
    legs, _ = run(FRIDAY, weekend_gap(out_km=60), history=[])
    distances = Distances({loc.id: loc for loc in LOCATIONS})
    distances.learn(legs[0])
    assert distances.history("home", "client") == []


def test_k_first_trip_to_new_client():
    legs, segments = run(WORKDAY, normal_day(40, "client2", out_km=41))
    assert [s.reason for s in segments] == [Reason.FIRST_TRIP, Reason.FITS]
    assert legs[1].expected_km == 41  # learned from the trip out, the same day
    assert business(legs) == 81


def test_l_first_trip_not_plausible():
    legs, segments = run(WORKDAY, normal_day(31, "client2", out_km=75))
    first = segments[0]
    assert (first.rule, first.reason, first.question) == (
        2,
        Reason.IMPLAUSIBLE,
        Question.BUSINESS_OR_PRIVATE,
    )
    assert FLAG_IMPLAUSIBLE in legs[0].flags
    assert segments[1].reason is Reason.FIRST_TRIP  # 75 is not learned
    assert business(legs) == 31
    apply_answer(first, Answer.BUSINESS)
    assert business(legs) == 106


def test_m_stop_without_history():
    legs, segments = run(
        WORKDAY,
        [
            ("00:00", "07:30", "home", START_KM),
            ("08:00", "09:10", "unknown", START_KM + 20),
            ("09:40", "16:00", "client2", START_KM + 45),
        ],
    )
    seg = segments[0]
    assert (seg.rule, seg.reason, seg.question) == (3, Reason.STOP_CROW, None)
    assert business(legs) == 30


# --- expected distance --------------------------------------------------------------------


def _distances(kms, first=date(2026, 9, 1)):
    d = Distances({loc.id: loc for loc in LOCATIONS})
    for leg in past_trips(kms, before=first):
        d.learn(leg)
    return d


def test_expected_is_the_most_frequent_ties_lowest():
    assert _distances([70, 69, 70, 84, 71, 70]).expected("home", "client") == 70
    assert _distances([69, 70]).expected("client", "home") == 69
    assert _distances([]).expected("home", "client") is None


def test_expected_uses_the_last_ten_trips():
    assert _distances([60] * 5 + [70] * 10).expected("home", "client") == 70


def test_expected_only_from_earlier_trips():
    d = _distances([70, 70, 84, 84, 84])  # one per day, 27..31 aug
    assert d.expected("home", "client", before=datetime(2026, 8, 30, tzinfo=AMSTERDAM)) == 70
    assert d.expected("home", "client", before=datetime(2026, 9, 1, tzinfo=AMSTERDAM)) == 84


def test_implausible_trips_are_not_learned():
    # straight-line 50 km: plausible is 48..102
    assert _distances([47, 103, 103, 70]).history("home", "client") == [70]


def test_margin_is_15_percent_at_least_2_km():
    assert margin(70) == Decimal("10.50")
    assert margin(8) == 2


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
