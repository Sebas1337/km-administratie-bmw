"""Answers and corrections survive re-derivation (ONTWERP §7.2, example days §7.3)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from kmlog import actions
from kmlog.derive import FLAG_POLL_GAP
from kmlog.model import LegStatus, Location, LocationType
from kmlog.process import rederive, reprocess
from kmlog.store.sqlite import SqliteStore
from test_aws_handlers import seeded
from test_voorbeelddagen import UNKNOWN, WORKDAY, normal_day, observations, weekend_gap

EVENING = datetime(2026, 10, 7, 21, 30, tzinfo=UTC)


def day_c():
    return [
        ("00:00", "07:30", "home", 40_000),
        ("08:30", "16:30", "client", 40_071),
        ("17:40", "21:10", "unknown", 40_109),
        ("22:00", "23:00", "home", 40_160),
    ]


def business(legs) -> int:
    return int(sum(leg.business_km for leg in legs))


def test_day_g_answer_survives_rederive():
    store = seeded(SqliteStore())
    store.put_observations(observations(WORKDAY, normal_day(96)))
    legs = rederive(store, "car", EVENING)
    assert business(legs) == 141
    open_leg = legs[1]
    assert open_leg.question == "detour_or_private_stop"
    assert open_leg.status is LegStatus.NEEDS_CONFIRMATION
    assert open_leg.business_if_confirmed == 96

    store.put_answer("car", open_leg.segment_id, "detour", EVENING)
    for _ in range(2):  # every hourly poll re-derives; the answer must stick
        legs = rederive(store, "car", EVENING)
        assert business(legs) == 167
        assert legs[1].status is LegStatus.CONFIRMED
    assert business(store.legs("car", WORKDAY, WORKDAY)) == 167


def test_day_c_prime_place_saved_as_client():
    store = seeded(SqliteStore())
    store.put_observations(observations(WORKDAY, day_c()))
    legs = rederive(store, "car", EVENING)
    assert {leg.segment_id for leg in legs[1:]} == {legs[1].segment_id}
    assert legs[1].question is None and business(legs) == 141
    store.put_location(Location("shop", "Klant 3", LocationType.CLIENT, *UNKNOWN))
    assert business(rederive(store, "car", EVENING)) == 160


def test_unanswered_question_times_out_after_seven_days():
    store = seeded(SqliteStore())
    store.put_observations(observations(WORKDAY, normal_day(96)))
    legs = reprocess(store, "car", WORKDAY, WORKDAY, EVENING + timedelta(days=7))
    assert legs[1].status is LegStatus.NEEDS_CONFIRMATION
    legs = reprocess(store, "car", WORKDAY, WORKDAY, EVENING + timedelta(days=8))
    assert legs[1].status is LegStatus.TIMED_OUT
    assert business(legs) == 141  # conservative default stands


def test_override_survives_and_is_clamped():
    store = seeded(SqliteStore())
    store.put_observations(observations(WORKDAY, normal_day(96)))
    legs = rederive(store, "car", EVENING)
    store.put_override("car", legs[1].id, Decimal(80), "file A12")
    legs = rederive(store, "car", EVENING)
    assert (int(legs[1].business_km), int(legs[1].private_km)) == (80, 16)
    assert legs[1].note == "file A12" and "manual_correction" in legs[1].flags
    store.put_override("car", legs[1].id, Decimal(500), None)
    assert int(rederive(store, "car", EVENING)[1].business_km) == 96


def test_stale_answer_is_ignored():
    store = seeded(SqliteStore())
    store.put_observations(observations(WORKDAY, normal_day(96)))
    legs = rederive(store, "car", EVENING)
    store.put_answer("car", legs[1].segment_id, "business", EVENING)  # not an option for rule 2
    legs = rederive(store, "car", EVENING)
    assert legs[1].question == "detour_or_private_stop"
    assert legs[1].status is LegStatus.NEEDS_CONFIRMATION
    assert business(legs) == 141


def test_weekend_question():
    store = seeded(SqliteStore())
    saturday = date(2026, 10, 10)
    store.put_observations(observations(saturday, normal_day(70)))
    now = datetime(2026, 10, 10, 21, 30, tzinfo=UTC)
    legs = rederive(store, "car", now)
    assert {leg.question for leg in legs} == {"weekend_or_holiday"}
    store.put_answer("car", legs[0].segment_id, "private", now)
    store.put_answer("car", legs[1].segment_id, "confirm", now)
    legs = rederive(store, "car", now)
    assert [int(leg.business_km) for leg in legs] == [0, 70]
    assert all(leg.status is LegStatus.CONFIRMED for leg in legs)


def test_hourly_rederive_keeps_the_leg_across_a_poll_gap():
    store = seeded(SqliteStore())
    friday, monday, tuesday = date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)
    store.put_observations(observations(friday, weekend_gap()))
    # Tuesday's hourly polls re-derive Monday and Tuesday from Saturday on: the departure
    # (Friday 19:00) is only found through observation_before
    rederive(store, "car", datetime(2026, 10, 13, 8, tzinfo=UTC))
    legs = store.legs("car", friday, tuesday)
    assert [(leg.date, leg.flags, int(leg.business_km)) for leg in legs] == [
        (monday, [FLAG_POLL_GAP], 70),
        (monday, [], 69),
    ]


def test_manual_trip_takes_over_a_poll_gap():
    store = seeded(SqliteStore())
    monday = date(2026, 10, 12)
    now = datetime(2026, 10, 12, 21, tzinfo=UTC)
    store.put_observations(observations(date(2026, 10, 9), weekend_gap()))
    rederive(store, "car", now)
    assert business(store.legs("car", monday, monday)) == 139

    trip = actions.add_manual_trip(store, "car", monday, Decimal(69), "Thuis → Klant", None, now)
    assert business(store.legs("car", monday, monday)) == 69 + 69  # gap leg now private
    gap = next(leg for leg in store.legs("car", monday, monday) if FLAG_POLL_GAP in leg.flags)
    assert (gap.business_km, gap.reason) == (0, "gap_manual")

    actions.delete_manual_trip(store, "car", trip.id, monday, now)
    assert business(store.legs("car", monday, monday)) == 139
