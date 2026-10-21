"""Dry run of the classification on a copy: what would change if the legs were re-derived now?

Copies locations, observations, stored legs (as history), answers and overrides from a store
(e.g. the live DynamoDB table, read only) into an in-memory SQLite store, re-derives the given
days there with the current code, and lists every leg whose outcome differs from the stored
one. Use it before deploying a change to the rules (AGENTS.md, Versions). No CarData calls, and
nothing is written to the source store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from .derive import AMSTERDAM
from .model import Leg, LegSource
from .process import CONTEXT_DAYS, HISTORY_DAYS, reprocess
from .store import Store
from .store.sqlite import SqliteStore


@dataclass
class Change:
    day: date
    leg_id: str
    description: str
    before: Leg | None
    after: Leg | None

    @property
    def business_delta(self) -> Decimal:
        old = self.before.business_km if self.before else Decimal(0)
        new = self.after.business_km if self.after else Decimal(0)
        return new - old


def _outcome(leg: Leg | None) -> tuple:
    if leg is None:
        return ()
    return (leg.distance_km, leg.business_km, leg.status, leg.question)


def compare(source: Store, vehicle: str, first: date, last: date, now: datetime) -> list[Change]:
    copy = SqliteStore()
    for loc in source.locations():
        copy.put_location(loc)
    start = datetime.combine(first - timedelta(days=CONTEXT_DAYS), time(), AMSTERDAM)
    end = datetime.combine(last + timedelta(days=2), time(), AMSTERDAM)
    copy.put_observations(source.observations(vehicle, start=start, end=end))
    stored = source.legs(vehicle, first - timedelta(days=HISTORY_DAYS), last)
    for source_kind in LegSource:
        legs = [leg for leg in stored if leg.source is source_kind]
        if legs:
            copy.replace_legs(vehicle, source_kind, legs[0].date, last, legs)
    for segment_id, answer in source.answers(vehicle).items():
        copy.put_answer(vehicle, segment_id, answer, now)
    for leg_id, (km, note) in source.overrides(vehicle).items():
        copy.put_override(vehicle, leg_id, km, note)

    after = {leg.id: leg for leg in reprocess(copy, vehicle, first, last, now)}
    before = {
        leg.id: leg
        for leg in stored
        if first <= leg.date <= last and leg.source is LegSource.CARDATA
    }
    changes = []
    for leg_id in sorted(set(before) | set(after)):
        old, new = before.get(leg_id), after.get(leg_id)
        if _outcome(old) != _outcome(new):
            leg = new or old
            route = f"{leg.from_location or 'onbekend'} → {leg.to_location or 'onbekend'}"
            changes.append(Change(leg.date, leg_id, route, old, new))
    return changes
