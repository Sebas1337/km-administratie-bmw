"""Re-derive and classify CarData legs from the stored observations.

Idempotent: legs are rebuilt from observations and replace the stored CarData legs of the
affected days (manual legs are untouched). Confirmation answers (per
segment) and manual corrections (per leg) live in their own store records and are re-applied
on every run, so they survive re-derivation; questions older than CONFIRM_TIMEOUT_DAYS get
status TIMED_OUT (the conservative default stands). Leg and segment ids are stable: vehicle +
departure time of the (first) leg.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from .classify import CONFIRM_TIMEOUT_DAYS, apply_stored_answer, classify_legs, time_out
from .derive import AMSTERDAM, amsterdam_date, derive_episodes, derive_legs
from .model import Leg, LegSource, LegStatus
from .store import Store

# Observations from this many days before the first re-derived day give the episode the car
# was parked in (e.g. the night at home) and the start of a night shift.
CONTEXT_DAYS = 2
# Earlier legs to learn the expected distances from (ONTWERP §7.2: last 10 trips per pair).
HISTORY_DAYS = 365
FLAG_MANUAL = "manual_correction"


def reprocess(store: Store, vehicle: str, first: date, last: date, now: datetime) -> list[Leg]:
    """Rebuild the CarData legs dated first..last (Amsterdam days) and store them."""
    since = datetime.combine(first - timedelta(days=CONTEXT_DAYS), time(), AMSTERDAM)
    until = datetime.combine(last + timedelta(days=2), time(), AMSTERDAM)  # night shifts
    locations = store.locations()
    history = store.legs(vehicle, first - timedelta(days=HISTORY_DAYS), first - timedelta(days=1))

    observations = store.observations(vehicle, start=since, end=until)
    # the episode the car was parked in, however long ago: after a poll gap of days the
    # departure lies before `since` (ONTWERP §7.1)
    before = store.observation_before(vehicle, since)
    if before is not None:
        observations.insert(0, before)
    episodes = derive_episodes(observations, locations)
    polled = [o.polled_at for o in observations]
    legs = derive_legs(episodes, locations, polled, store.settings())
    manual_from = amsterdam_date(observations[0].polled_at) if observations else first
    manual_days = {
        leg.date
        for leg in store.legs(vehicle, manual_from, last + timedelta(days=2))
        if leg.source is LegSource.MANUAL and not leg.imported
    }
    segments = classify_legs(legs, locations, history, manual_days)

    answers = store.answers(vehicle)
    overrides = store.overrides(vehicle)
    deadline = amsterdam_date(now) - timedelta(days=CONFIRM_TIMEOUT_DAYS)
    for seg in segments:
        answer = answers.get(seg.legs[0].id)
        if not (answer and apply_stored_answer(seg, answer)) and seg.legs[0].date < deadline:
            time_out(seg)
        for leg in seg.legs:
            if leg.id in overrides:
                apply_override(leg, *overrides[leg.id])

    keep = [leg for leg in legs if first <= leg.date <= last]
    store.replace_legs(vehicle, LegSource.CARDATA, first, last, keep)
    return keep


def apply_override(leg: Leg, business_km: Decimal, note: str | None) -> None:
    business = max(Decimal(0), min(business_km, leg.distance_km))
    leg.business_km = business
    leg.private_km = leg.distance_km - business
    leg.status = LegStatus.CONFIRMED
    if FLAG_MANUAL not in leg.flags:
        leg.flags.append(FLAG_MANUAL)
    if note:
        leg.note = note


def rederive(store: Store, vehicle: str, now: datetime, days: int = 2) -> list[Leg]:
    """Rebuild the CarData legs dated on the last `days` Amsterdam days (today included)."""
    today = amsterdam_date(now)
    return reprocess(store, vehicle, today - timedelta(days=days - 1), today, now)


def day_range(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    nxt = date(year + month // 12, month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)
