"""Stamdata (vehicle, locations, routes, rates) from a TOML file. See docs/ONTWERP.md §7.2.

The real file is data/stamdata.toml (gitignored: it holds home and client coordinates);
data/samples/stamdata.example.toml shows the format.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .model import Location, LocationType, Rate, Route, Vehicle, VehicleKind

DEFAULT_PATH = Path("data/stamdata.toml")


@dataclass
class Stamdata:
    locations: list[Location] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    rates: list[Rate] = field(default_factory=list)
    vehicle: Vehicle | None = None  # the vin field holds the vehicle id (kmlog --vehicle)


def vehicle(vehicle_id: str, data: dict) -> Vehicle:
    return Vehicle(
        vin=vehicle_id,
        name=str(data.get("name", vehicle_id)),
        plate=str(data.get("plate", "")),
        kind=VehicleKind.CAR,
        reimbursable=True,
    )


def _date(value) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def parse(data: dict, vehicle_id: str = "car") -> Stamdata:
    result = Stamdata()
    if "vehicle" in data:
        result.vehicle = vehicle(vehicle_id, data["vehicle"])
    for loc in data.get("location", []):
        result.locations.append(
            Location(
                id=loc["id"],
                name=loc.get("name", loc["id"]),
                type=LocationType(loc["type"]),
                lat=float(loc["lat"]),
                lon=float(loc["lon"]),
                radius_m=int(loc.get("radius_m", 200)),
                active_from=_date(loc.get("active_from")),
                active_to=_date(loc.get("active_to")),
            )
        )
    ids = {loc.id for loc in result.locations}
    if len(ids) != len(result.locations):
        raise ValueError("duplicate location id")
    for r in data.get("route", []):
        route = Route(
            id=r["id"],
            location_a=r["from"],
            location_b=r["to"],
            name=r.get("name", r["id"]),
            distance_km=int(r["km"]),
            is_default=bool(r.get("default", False)),
        )
        unknown = {route.location_a, route.location_b} - ids
        if unknown:
            raise ValueError(f"route {route.id}: unknown location(s) {sorted(unknown)}")
        result.routes.append(route)
    for a, b in {frozenset((r.location_a, r.location_b)) for r in result.routes}:
        defaults = [r.id for r in result.routes if r.connects(a, b) and r.is_default]
        if len(defaults) > 1:
            raise ValueError(f"more than one default route between {a} and {b}: {defaults}")
    for rate in data.get("rate", []):
        result.rates.append(Rate(year=int(rate["year"]), cents_per_km=int(rate["cents_per_km"])))
    return result


def load(path: str | Path = DEFAULT_PATH, vehicle_id: str = "car") -> Stamdata:
    with open(path, "rb") as f:
        return parse(tomllib.load(f), vehicle_id)


# --- export (the web UI makes the store the source of truth) --------------------------------


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid TOML string


def dumps(data: Stamdata) -> str:
    lines = [
        "# Stamdata, geëxporteerd uit de database (kmlog stamdata --export).",
        "# Inlezen: uv run kmlog stamdata <bestand> [--force]",
        "",
    ]
    if data.vehicle:
        v = data.vehicle
        lines += [
            "[vehicle]",
            f"name = {_toml_value(v.name)}",
            f"plate = {_toml_value(v.plate)}",
            "",
        ]
    for loc in data.locations:
        fields = {
            "id": loc.id,
            "name": loc.name,
            "type": loc.type.value,
            "lat": loc.lat,
            "lon": loc.lon,
            "radius_m": loc.radius_m,
        }
        if loc.active_from:
            fields["active_from"] = loc.active_from.isoformat()
        if loc.active_to:
            fields["active_to"] = loc.active_to.isoformat()
        lines += ["[[location]]", *(f"{k} = {_toml_value(v)}" for k, v in fields.items()), ""]
    for r in data.routes:
        fields = {
            "id": r.id,
            "from": r.location_a,
            "to": r.location_b,
            "name": r.name,
            "km": r.distance_km,
            "default": r.is_default,
        }
        lines += ["[[route]]", *(f"{k} = {_toml_value(v)}" for k, v in fields.items()), ""]
    for rate in data.rates:
        lines += ["[[rate]]", f"year = {rate.year}", f"cents_per_km = {rate.cents_per_km}", ""]
    return "\n".join(lines)


def same_places(a: Stamdata, b: Stamdata) -> bool:
    def key(d: Stamdata):
        return (
            sorted(d.locations, key=lambda loc: loc.id),
            sorted(d.routes, key=lambda r: r.id),
        )

    return key(a) == key(b)
