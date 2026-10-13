"""Domain model, per ONTWERP.md §8.

Conventions: money in integer cents, odometer values as int km,
distances as Decimal km, all timestamps stored in UTC, a "day" is a calendar day
in Europe/Amsterdam derived from the departure time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class VehicleKind(StrEnum):
    CAR = "car"
    MOTOR = "motor"


class LocationType(StrEnum):
    HOME = "home"
    CLIENT = "client"
    BUSINESS = "business"
    PRIVATE = "private"


class Classification(StrEnum):
    BUSINESS = "business"
    PRIVATE = "private"
    MIXED = "mixed"


class LegStatus(StrEnum):
    AUTO = "auto"
    NEEDS_CONFIRMATION = "needs_confirmation"
    CONFIRMED = "confirmed"
    TIMED_OUT = "timed_out"


class LegSource(StrEnum):
    CARDATA = "cardata"
    MANUAL = "manual"

    @classmethod
    def _missing_(cls, value):
        # older databases hold trips imported from a spreadsheet ("excel_2026"); they have no
        # odometer or times, just like manual trips, and are shown and counted as such
        return cls.MANUAL if isinstance(value, str) and value.startswith("excel") else None


@dataclass(frozen=True)
class Vehicle:
    vin: str
    name: str
    plate: str
    kind: VehicleKind
    reimbursable: bool
    active_from: date | None = None
    active_to: date | None = None


@dataclass(frozen=True)
class Settings:
    """Settings from the web UI (Instellingen) that are not stamdata.

    The poller is invoked every hour on the hour; it only calls BMW within the poll window
    (Europe/Amsterdam hours, both ends included; end < start runs past midnight).
    """

    poll_start_hour: int = 6
    poll_end_hour: int = 23

    @property
    def poll_hours(self) -> list[int]:
        start, end = self.poll_start_hour, self.poll_end_hour
        return list(range(start, end + 1)) if start <= end else [*range(start, 24), *range(end + 1)]

    def polls_at(self, hour: int) -> bool:
        return hour in self.poll_hours


@dataclass(frozen=True)
class Rate:
    """Reimbursement rate for a calendar year, in integer cents per km."""

    year: int
    cents_per_km: int

    def amount_cents(self, km: int) -> int:
        return km * self.cents_per_km


@dataclass(frozen=True)
class Location:
    id: str
    name: str
    type: LocationType
    lat: float
    lon: float
    radius_m: int = 200
    active_from: date | None = None
    active_to: date | None = None


@dataclass(frozen=True)
class Route:
    """Known route between an unordered pair of locations."""

    id: str
    location_a: str
    location_b: str
    name: str
    distance_km: int
    is_default: bool = False

    def connects(self, loc1: str, loc2: str) -> bool:
        return {self.location_a, self.location_b} == {loc1, loc2}


@dataclass(frozen=True)
class Observation:
    """One poll of the CarData API. odometer_ts is when the car reported the value,
    polled_at is when we asked; derivation uses odometer_ts."""

    vehicle: str
    polled_at: datetime
    odometer_km: int
    odometer_ts: datetime
    lat: float | None
    lon: float | None
    loc_ts: datetime | None
    raw: dict | None = None


@dataclass
class ParkingEpisode:
    """Consecutive observations with the same odometer value: the car is parked."""

    vehicle: str
    odometer_km: int
    lat: float | None
    lon: float | None
    first_seen: datetime
    last_seen: datetime
    location_id: str | None = None
    # set by derivation when this is not a real stop but a reading taken while driving
    pass_through: str | None = None


@dataclass(frozen=True)
class Waypoint:
    """A reading taken while the car was on its way (ONTWERP §7.1): kept on the leg for the
    day page, not a stop."""

    seen_at: datetime
    odometer_km: int
    lat: float | None
    lon: float | None
    reason: str  # derive.PASS_SHORT


@dataclass
class Leg:
    """Movement between two parking episodes; the unit of classification."""

    id: str
    vehicle: str
    date: date  # Amsterdam calendar day of departure
    distance_km: Decimal
    departed_after: datetime | None = None
    arrived_at: datetime | None = None
    from_location: str | None = None
    to_location: str | None = None
    from_latlon: tuple[float, float] | None = None
    to_latlon: tuple[float, float] | None = None
    matched_route: str | None = None
    business_km: Decimal = Decimal(0)
    private_km: Decimal = Decimal(0)
    classification: Classification = Classification.PRIVATE
    status: LegStatus = LegStatus.AUTO
    flags: list[str] = field(default_factory=list)
    note: str | None = None
    source: LegSource = LegSource.CARDATA
    # classification context, so the UI and the notify job can ask about a whole segment
    segment_id: str | None = None  # id of the first leg of the segment (ONTWERP §7.2)
    question: str | None = None  # classify.Question value while an answer is possible
    business_if_confirmed: Decimal | None = None
    # odometer (km) when parked before departure and after arrival; None for manual legs
    odometer_from: int | None = None
    odometer_to: int | None = None
    waypoints: list[Waypoint] = field(default_factory=list)

    @property
    def imported(self) -> bool:
        """Read from an older database's spreadsheet import (see LegSource._missing_)."""
        return self.source is LegSource.MANUAL and "#excel#" in self.id


@dataclass(frozen=True)
class DayNote:
    """Note on a day without a trip (Vakantie, Motor, feestdag, ...)."""

    vehicle: str
    date: date
    text: str


@dataclass
class ConfirmationRequest:
    id: str
    leg_id: str
    question: str
    options: list[str]
    sent_at: datetime
    channel: str
    token_hash: str
    answered_at: datetime | None = None
    answer: str | None = None
