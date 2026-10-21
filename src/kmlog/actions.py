"""User actions from the web UI: answer questions, correct km, manual trips, stamdata.

Every action that changes how CarData legs are classified stores its input separately (answer,
override, location) and then re-derives the affected days from the stored observations,
so the result survives the next hourly poll. No CarData API call is ever made here.
"""

from __future__ import annotations

import dataclasses
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from .classify import OPTIONS, Answer, Distances, Question
from .derive import amsterdam_date
from .model import (
    Classification,
    Leg,
    LegSource,
    LegStatus,
    Location,
    LocationType,
    Rate,
    Settings,
    Vehicle,
    VehicleKind,
)
from .process import HISTORY_DAYS, reprocess
from .store import Store

# a segment id is the id of its first leg: "<vehicle>#YYYYmmddTHHMM" (Amsterdam departure)
_SEGMENT_DAY = re.compile(r"#(\d{8})T\d{4}$")


class ActionError(ValueError):
    """Invalid user input; the message is shown to the user (Dutch)."""


@dataclass
class OpenQuestion:
    segment_id: str
    question: Question
    legs: list[Leg] = field(default_factory=list)

    @property
    def date(self) -> date:
        return self.legs[0].date

    @property
    def distance_km(self) -> int:
        return int(sum(leg.distance_km for leg in self.legs))

    @property
    def business_km(self) -> int:
        return int(sum(leg.business_km for leg in self.legs))

    @property
    def business_if_confirmed(self) -> int:
        return int(self.legs[0].business_if_confirmed or 0)

    @property
    def options(self) -> tuple[Answer, ...]:
        return OPTIONS[self.question]


def open_questions(legs: list[Leg]) -> list[OpenQuestion]:
    """Segments that still wait for an answer, oldest first."""
    found: dict[str, OpenQuestion] = {}
    for leg in sorted(legs, key=lambda leg: (leg.date, leg.id)):
        if leg.status is not LegStatus.NEEDS_CONFIRMATION or not leg.question:
            continue
        key = leg.segment_id or leg.id
        q = found.setdefault(key, OpenQuestion(key, Question(leg.question)))
        q.legs.append(leg)
    return list(found.values())


def _segment_window(segment_id: str) -> tuple[date, date]:
    m = _SEGMENT_DAY.search(segment_id)
    if not m:
        raise ActionError("Onbekende rit.")
    d = datetime.strptime(m.group(1), "%Y%m%d").date()
    return d - timedelta(days=1), d + timedelta(days=1)


def segment_legs(store: Store, vehicle: str, segment_id: str) -> list[Leg]:
    lo, hi = _segment_window(segment_id)
    legs = [leg for leg in store.legs(vehicle, lo, hi) if leg.segment_id == segment_id]
    if not legs:
        raise ActionError("Deze rit bestaat niet (meer).")
    return legs


def _reprocess_around(store: Store, vehicle: str, legs: list[Leg], now: datetime) -> None:
    days = [leg.date for leg in legs]
    reprocess(store, vehicle, min(days), max(days), now)


def answer(store: Store, vehicle: str, segment_id: str, value: str, now: datetime) -> None:
    legs = segment_legs(store, vehicle, segment_id)
    question = legs[0].question
    try:
        parsed = Answer(value)
    except ValueError as e:
        raise ActionError("Onbekend antwoord.") from e
    if not question or parsed not in OPTIONS[Question(question)]:
        raise ActionError("Dit antwoord past niet bij deze vraag.")
    store.put_answer(vehicle, segment_id, parsed.value, now)
    _reprocess_around(store, vehicle, legs, now)


def undo_answer(store: Store, vehicle: str, segment_id: str, now: datetime) -> None:
    legs = segment_legs(store, vehicle, segment_id)
    store.delete_answer(vehicle, segment_id)
    _reprocess_around(store, vehicle, legs, now)


def _find_leg(store: Store, vehicle: str, leg_id: str, day: date) -> Leg:
    for leg in store.legs(vehicle, day, day):
        if leg.id == leg_id:
            return leg
    raise ActionError("Deze rit bestaat niet (meer).")


def parse_km(text: str, *, allow_zero: bool = True) -> Decimal:
    try:
        km = Decimal(text.strip().replace(",", "."))
    except Exception as e:
        raise ActionError("Vul een geldig aantal kilometers in.") from e
    if km != km.to_integral_value() or km < 0 or (km == 0 and not allow_zero) or km > 2000:
        raise ActionError("Kilometers: een heel getal tussen 0 en 2000.")
    return km


def correct(
    store: Store,
    vehicle: str,
    leg_id: str,
    day: date,
    business_km: Decimal,
    note: str | None,
    now: datetime,
) -> None:
    leg = _find_leg(store, vehicle, leg_id, day)
    if business_km > leg.distance_km:
        raise ActionError(f"Zakelijk kan niet meer zijn dan de gereden {int(leg.distance_km)} km.")
    if leg.source is LegSource.CARDATA:
        store.put_override(vehicle, leg.id, business_km, note or None)
        reprocess(store, vehicle, day, day, now)
        return
    leg.business_km = business_km
    leg.private_km = leg.distance_km - business_km
    leg.classification = (
        Classification.BUSINESS
        if business_km == leg.distance_km
        else Classification.PRIVATE
        if business_km == 0
        else Classification.MIXED
    )
    leg.status = LegStatus.CONFIRMED
    if note:
        leg.note = note
    store.put_leg(leg)


def undo_correction(store: Store, vehicle: str, leg_id: str, day: date, now: datetime) -> None:
    store.delete_override(vehicle, leg_id)
    reprocess(store, vehicle, day, day, now)


def add_manual_trip(
    store: Store, vehicle: str, day: date, km: Decimal, description: str, note: str | None
) -> Leg:
    if km <= 0:
        raise ActionError("Een handmatige rit heeft minstens 1 km.")
    if not description.strip():
        raise ActionError("Geef een omschrijving, bijv. 'Thuis → Klant → Thuis'.")
    leg = Leg(
        id=f"{vehicle}#manual#{secrets.token_hex(6)}",
        vehicle=vehicle,
        date=day,
        distance_km=km,
        business_km=km,
        classification=Classification.BUSINESS,
        status=LegStatus.CONFIRMED,
        note="; ".join(x for x in (description.strip(), (note or "").strip()) if x),
        source=LegSource.MANUAL,
    )
    store.put_leg(leg)
    return leg


def delete_manual_trip(store: Store, vehicle: str, leg_id: str, day: date) -> None:
    leg = _find_leg(store, vehicle, leg_id, day)
    if leg.source is not LegSource.MANUAL or leg.imported:
        raise ActionError("Alleen handmatige ritten kunnen worden verwijderd.")
    store.delete_leg(leg)


# --- stamdata -----------------------------------------------------------------------------


def slug(text: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-") or "locatie"


def _unique_id(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def save_location(
    store: Store,
    *,
    name: str,
    type_: str,
    lat: float,
    lon: float,
    radius_m: int = 200,
    id_: str | None = None,
) -> Location:
    name = name.strip()
    if not name:
        raise ActionError("Geef de locatie een naam.")
    try:
        loc_type = LocationType(type_)
    except ValueError as e:
        raise ActionError("Onbekend type locatie.") from e
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ActionError("Ongeldige coördinaten.")
    if not 25 <= radius_m <= 2000:
        raise ActionError("Straal: tussen 25 en 2000 meter.")
    existing = {loc.id: loc for loc in store.locations()}
    if loc_type is LocationType.HOME and any(
        loc.type is LocationType.HOME and loc.id != id_ for loc in existing.values()
    ):
        raise ActionError("Er is al een thuislocatie.")
    if id_ is None:
        id_ = _unique_id(slug(name), set(existing))
    elif id_ not in existing:
        raise ActionError("Onbekende locatie.")
    loc = Location(id=id_, name=name, type=loc_type, lat=lat, lon=lon, radius_m=radius_m)
    store.put_location(loc)
    return loc


def delete_location(store: Store, id_: str) -> None:
    locations = [loc for loc in store.locations() if loc.id != id_]
    routes = [r for r in store.routes() if id_ not in (r.location_a, r.location_b)]  # pre-v0.3.0
    store.replace_places(locations, routes)


def first_of_previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def save_place(
    store: Store,
    vehicle: str,
    leg_id: str,
    day: date,
    name: str,
    type_: str,
    now: datetime,
) -> Location:
    """Save the unknown place a leg arrived at as a location (ONTWERP §9, "Plek opslaan").

    The legs are re-derived from the 1st of the previous month (or the leg's day, if older),
    so a place from the monthly mail counts too. As a client/business place, trips to it are
    first trips (rule 1 when plausible) and then form the expected distance; as a private place
    it only leaves the "onbekende plekken" list.
    """
    if type_ not in (LocationType.CLIENT, LocationType.BUSINESS, LocationType.PRIVATE):
        raise ActionError("Kies klant, zakelijk of privé.")
    leg = _find_leg(store, vehicle, leg_id, day)
    if leg.source is not LegSource.CARDATA or leg.to_location is not None or not leg.to_latlon:
        raise ActionError("Deze rit eindigt niet op een onbekende plek.")
    loc = save_location(store, name=name, type_=type_, lat=leg.to_latlon[0], lon=leg.to_latlon[1])
    today = amsterdam_date(now)
    reprocess(store, vehicle, min(leg.date, first_of_previous_month(today)), today, now)
    return loc


def learned_distances(store: Store, vehicle: str, today: date) -> list[tuple[str, str, int, int]]:
    """(a, b, expected km, trips) per pair of business places, as classification sees them now."""
    locations = {loc.id: loc for loc in store.locations()}
    distances = Distances(locations)
    for leg in store.legs(vehicle, today - timedelta(days=HISTORY_DAYS), today):
        distances.learn(leg)
    return distances.pairs()


def save_vehicle(store: Store, vehicle_id: str, *, name: str, plate: str) -> Vehicle:
    name, plate = name.strip(), plate.strip().upper()
    if not name or len(name) > 60 or len(plate) > 20:
        raise ActionError("Geef de auto een naam (max. 60 tekens) en een geldig kenteken.")
    vehicle = Vehicle(vehicle_id, name, plate, VehicleKind.CAR, True)
    store.put_vehicle(vehicle)
    return vehicle


def save_rate(store: Store, *, year: int, euros: str) -> Rate:
    try:
        amount = Decimal(euros.strip().replace("€", "").replace(",", "."))
    except InvalidOperation as e:
        raise ActionError("Ongeldig bedrag; bijv. 0,23.") from e
    cents = amount * 100
    if not (0 < amount < 10) or cents != cents.to_integral_value():
        raise ActionError("Vergoeding per km: tussen € 0,01 en € 9,99, in hele centen.")
    if not 2000 <= year <= 2100:
        raise ActionError("Ongeldig jaar.")
    rate = Rate(year, int(cents))
    store.put_rate(rate)
    return rate


def delete_rate(store: Store, year: int) -> None:
    if store.rate(year) is None:
        raise ActionError("Voor dat jaar is geen tarief ingesteld.")
    store.delete_rate(year)


def save_poll_window(store: Store, *, start_hour: int, end_hour: int, quota_limit: int) -> Settings:
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        raise ActionError("Kies hele uren van 0 tot en met 23.")
    current = store.settings()
    settings = Settings(poll_start_hour=start_hour, poll_end_hour=end_hour)
    polls, room = len(settings.poll_hours), quota_limit - 5
    if polls > room:
        raise ActionError(
            f"Dat zijn {polls} metingen per dag; er passen er {room} in het dagquotum. "
            "Kies een korter venster."
        )
    store.put_settings(dataclasses.replace(current, **dataclasses.asdict(settings)))
    return settings


def save_mail_settings(store: Store, *, missed_days: bool) -> Settings:
    settings = dataclasses.replace(store.settings(), mail_missed_days=missed_days)
    store.put_settings(settings)
    return settings
