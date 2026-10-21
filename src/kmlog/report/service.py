"""Build maandstaten from a Store (shared by the CLI and the report Lambda)."""

from __future__ import annotations

from ..model import Rate
from ..process import day_range
from ..store import Store
from .maandstaat import Maandstaat, build_maandstaat


class MissingConfig(LookupError):
    pass


def rate_for(store: Store, year: int) -> Rate | None:
    """The rate of `year`, or else the most recent earlier one (a new year starts without one)."""
    rate = store.rate(year)
    if rate is None:
        earlier = [r for r in store.rates() if r.year < year]
        rate = Rate(year, earlier[-1].cents_per_km) if earlier else None
    return rate


def maandstaat_from_store(store: Store, vehicle_id: str, year: int, month: int) -> Maandstaat:
    vehicle = store.vehicle(vehicle_id)
    rate = rate_for(store, year)
    if vehicle is None or rate is None:
        raise MissingConfig(
            f"voertuig '{vehicle_id}' of tarief {year} ontbreekt; laad eerst de stamdata "
            "(kmlog stamdata)"
        )
    first, last = day_range(year, month)
    return build_maandstaat(
        year,
        month,
        vehicle,
        rate,
        store.legs(vehicle_id, first, last),
        store.day_notes(vehicle_id, first, last),
        store.locations(),
        store.observations(vehicle_id),
    )


def year_to_date(store: Store, vehicle_id: str, year: int, until_month: int) -> list[Maandstaat]:
    months = [maandstaat_from_store(store, vehicle_id, year, m) for m in range(1, until_month + 1)]
    return [m for m in months if m.rows]
