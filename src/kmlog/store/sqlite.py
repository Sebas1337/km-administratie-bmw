"""SQLite implementation of the Store: key columns for querying, the record itself as JSON."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from ..model import (
    DayNote,
    Leg,
    LegSource,
    Location,
    Observation,
    Rate,
    Route,
    SentReport,
    Settings,
    Vehicle,
)
from .codec import dumps, loads

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (kind, id)
);
CREATE TABLE IF NOT EXISTS observations (
    vehicle TEXT NOT NULL, polled_at TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (vehicle, polled_at)
);
CREATE TABLE IF NOT EXISTS legs (
    id TEXT PRIMARY KEY, vehicle TEXT NOT NULL, date TEXT NOT NULL, source TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS legs_by_date ON legs (vehicle, date);
CREATE TABLE IF NOT EXISTS day_notes (
    vehicle TEXT NOT NULL, date TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (vehicle, date)
);
CREATE TABLE IF NOT EXISTS quota (day TEXT PRIMARY KEY, used INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta (
    kind TEXT NOT NULL, vehicle TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (kind, vehicle, key)
);
CREATE TABLE IF NOT EXISTS login_tokens (hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY, used INTEGER NOT NULL, expires_at TEXT NOT NULL
);
"""


def _utc(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


class SqliteStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- stamdata -------------------------------------------------------------------------

    def _put_config(self, kind: str, id_: str, obj) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO config (kind, id, data) VALUES (?, ?, ?)",
                (kind, id_, dumps(obj)),
            )

    def _config(self, kind: str, cls, id_: str | None = None) -> list:
        sql, args = "SELECT data FROM config WHERE kind = ?", [kind]
        if id_ is not None:
            sql += " AND id = ?"
            args.append(id_)
        rows = self.db.execute(sql + " ORDER BY id", args).fetchall()
        return [loads(cls, data) for (data,) in rows]

    def put_vehicle(self, vehicle: Vehicle) -> None:
        self._put_config("vehicle", vehicle.vin, vehicle)

    def vehicle(self, vin: str) -> Vehicle | None:
        found = self._config("vehicle", Vehicle, vin)
        return found[0] if found else None

    def put_rate(self, rate: Rate) -> None:
        self._put_config("rate", str(rate.year), rate)

    def rate(self, year: int) -> Rate | None:
        found = self._config("rate", Rate, str(year))
        return found[0] if found else None

    def rates(self) -> list[Rate]:
        return sorted(self._config("rate", Rate), key=lambda r: r.year)

    def delete_rate(self, year: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM config WHERE kind = 'rate' AND id = ?", (str(year),))

    def put_settings(self, settings: Settings) -> None:
        self._put_config("settings", "main", settings)

    def settings(self) -> Settings:
        found = self._config("settings", Settings, "main")
        return found[0] if found else Settings()

    def put_location(self, location: Location) -> None:
        self._put_config("location", location.id, location)

    def locations(self) -> list[Location]:
        return self._config("location", Location)

    def put_route(self, route: Route) -> None:
        self._put_config("route", route.id, route)

    def routes(self) -> list[Route]:
        return self._config("route", Route)

    def replace_places(self, locations: Iterable[Location], routes: Iterable[Route]) -> None:
        """Replace all locations and routes at once (stamdata file is the source of truth)."""
        rows = [("location", loc.id, dumps(loc)) for loc in locations]
        rows += [("route", r.id, dumps(r)) for r in routes]
        with self.db:
            self.db.execute("DELETE FROM config WHERE kind IN ('location', 'route')")
            self.db.executemany("INSERT INTO config (kind, id, data) VALUES (?, ?, ?)", rows)

    # --- observations ---------------------------------------------------------------------

    def put_observations(self, observations: Iterable[Observation]) -> int:
        with self.db:
            cur = self.db.executemany(
                "INSERT OR IGNORE INTO observations (vehicle, polled_at, data) VALUES (?, ?, ?)",
                [(o.vehicle, _utc(o.polled_at), dumps(o)) for o in observations],
            )
        return cur.rowcount

    def observations(
        self, vehicle: str, start: datetime | None = None, end: datetime | None = None
    ) -> list[Observation]:
        sql, args = "SELECT data FROM observations WHERE vehicle = ?", [vehicle]
        if start is not None:
            sql += " AND polled_at >= ?"
            args.append(_utc(start))
        if end is not None:
            sql += " AND polled_at < ?"
            args.append(_utc(end))
        rows = self.db.execute(sql + " ORDER BY polled_at", args).fetchall()
        return [loads(Observation, data) for (data,) in rows]

    def observation_before(self, vehicle: str, before: datetime) -> Observation | None:
        row = self.db.execute(
            "SELECT data FROM observations WHERE vehicle = ? AND polled_at < ? "
            "ORDER BY polled_at DESC LIMIT 1",
            (vehicle, _utc(before)),
        ).fetchone()
        return loads(Observation, row[0]) if row else None

    # --- legs and day notes ---------------------------------------------------------------

    def replace_legs(
        self, vehicle: str, source: LegSource, start: date, end: date, legs: Iterable[Leg]
    ) -> None:
        legs = list(legs)
        for leg in legs:
            if leg.vehicle != vehicle or leg.source is not source or not start <= leg.date <= end:
                raise ValueError(f"leg {leg.id} is outside the range being replaced")
        with self.db:
            self.db.execute(
                "DELETE FROM legs WHERE vehicle = ? AND source = ? AND date BETWEEN ? AND ?",
                (vehicle, source.value, start.isoformat(), end.isoformat()),
            )
            self.db.executemany(
                "INSERT INTO legs (id, vehicle, date, source, data) VALUES (?, ?, ?, ?, ?)",
                [self._leg_row(leg) for leg in legs],
            )

    def put_leg(self, leg: Leg) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO legs (id, vehicle, date, source, data) "
                "VALUES (?, ?, ?, ?, ?)",
                self._leg_row(leg),
            )

    @staticmethod
    def _leg_row(leg: Leg) -> tuple:
        return (leg.id, leg.vehicle, leg.date.isoformat(), leg.source.value, dumps(leg))

    def legs(self, vehicle: str, start: date, end: date) -> list[Leg]:
        rows = self.db.execute(
            "SELECT data FROM legs WHERE vehicle = ? AND date BETWEEN ? AND ? ORDER BY date, id",
            (vehicle, start.isoformat(), end.isoformat()),
        ).fetchall()
        return [loads(Leg, data) for (data,) in rows]

    def replace_day_notes(
        self, vehicle: str, start: date, end: date, notes: Iterable[DayNote]
    ) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM day_notes WHERE vehicle = ? AND date BETWEEN ? AND ?",
                (vehicle, start.isoformat(), end.isoformat()),
            )
            self.db.executemany(
                "INSERT INTO day_notes (vehicle, date, data) VALUES (?, ?, ?)",
                [(n.vehicle, n.date.isoformat(), dumps(n)) for n in notes],
            )

    def day_notes(self, vehicle: str, start: date, end: date) -> list[DayNote]:
        rows = self.db.execute(
            "SELECT data FROM day_notes WHERE vehicle = ? AND date BETWEEN ? AND ? ORDER BY date",
            (vehicle, start.isoformat(), end.isoformat()),
        ).fetchall()
        return [loads(DayNote, data) for (data,) in rows]

    # --- quota ----------------------------------------------------------------------------

    def increment_quota(self, day: date) -> int:
        with self.db:
            self.db.execute(
                "INSERT INTO quota (day, used) VALUES (?, 1) "
                "ON CONFLICT (day) DO UPDATE SET used = used + 1",
                (day.isoformat(),),
            )
        return self.quota_used(day)

    def quota_used(self, day: date) -> int:
        row = self.db.execute("SELECT used FROM quota WHERE day = ?", (day.isoformat(),)).fetchone()
        return row[0] if row else 0

    def delete_leg(self, leg: Leg) -> None:
        with self.db:
            self.db.execute("DELETE FROM legs WHERE id = ?", (leg.id,))

    # --- confirmations, notifications -----------------------------------------------------

    def _put_meta(self, kind: str, vehicle: str, key: str, data: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO meta (kind, vehicle, key, data) VALUES (?, ?, ?, ?)",
                (kind, vehicle, key, json.dumps(data)),
            )

    def _delete_meta(self, kind: str, vehicle: str, key: str) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM meta WHERE kind = ? AND vehicle = ? AND key = ?", (kind, vehicle, key)
            )

    def _meta(self, kind: str, vehicle: str) -> dict[str, dict]:
        rows = self.db.execute(
            "SELECT key, data FROM meta WHERE kind = ? AND vehicle = ?", (kind, vehicle)
        ).fetchall()
        return {key: json.loads(data) for key, data in rows}

    def put_sent_report(self, vehicle: str, report: SentReport) -> None:
        data = {
            "sent_at": _utc(report.sent_at),
            "business_km": report.business_km,
            "amount_cents": report.amount_cents,
        }
        self._put_meta("report", vehicle, f"{report.year:04d}-{report.month:02d}", data)

    def sent_report(self, vehicle: str, year: int, month: int) -> SentReport | None:
        data = self._meta("report", vehicle).get(f"{year:04d}-{month:02d}")
        if data is None:
            return None
        sent_at = datetime.fromisoformat(data["sent_at"])
        return SentReport(year, month, sent_at, data["business_km"], data["amount_cents"])

    def put_answer(self, vehicle: str, segment_id: str, answer: str, at: datetime) -> None:
        self._put_meta("answer", vehicle, segment_id, {"answer": answer, "at": _utc(at)})

    def delete_answer(self, vehicle: str, segment_id: str) -> None:
        self._delete_meta("answer", vehicle, segment_id)

    def answers(self, vehicle: str) -> dict[str, str]:
        return {k: v["answer"] for k, v in self._meta("answer", vehicle).items()}

    def put_override(
        self, vehicle: str, leg_id: str, business_km: Decimal, note: str | None
    ) -> None:
        self._put_meta("override", vehicle, leg_id, {"business_km": str(business_km), "note": note})

    def delete_override(self, vehicle: str, leg_id: str) -> None:
        self._delete_meta("override", vehicle, leg_id)

    def overrides(self, vehicle: str) -> dict[str, tuple[Decimal, str | None]]:
        return {
            k: (Decimal(v["business_km"]), v.get("note"))
            for k, v in self._meta("override", vehicle).items()
        }

    def mark_notified(self, vehicle: str, keys: Iterable[str], at: datetime) -> None:
        for key in keys:
            self._put_meta("notified", vehicle, key, {"at": _utc(at)})

    def notified(self, vehicle: str) -> set[str]:
        return set(self._meta("notified", vehicle))

    # --- web login ------------------------------------------------------------------------

    def put_login_token(self, token_hash: str, expires_at: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO login_tokens (hash, expires_at) VALUES (?, ?)",
                (token_hash, _utc(expires_at)),
            )

    def consume_login_token(self, token_hash: str, now: datetime) -> bool:
        with self.db:
            cur = self.db.execute(
                "DELETE FROM login_tokens WHERE hash = ? AND expires_at > ?",
                (token_hash, _utc(now)),
            )
        return cur.rowcount == 1

    def increment_counter(self, key: str, expires_at: datetime) -> int:
        with self.db:
            self.db.execute(
                "INSERT INTO counters (key, used, expires_at) VALUES (?, 1, ?) "
                "ON CONFLICT(key) DO UPDATE SET used = used + 1",
                (key, _utc(expires_at)),
            )
        return self.db.execute("SELECT used FROM counters WHERE key = ?", (key,)).fetchone()[0]
