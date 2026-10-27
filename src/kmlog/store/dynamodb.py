"""DynamoDB single-table implementation of the Store (ONTWERP.md §8).

Keys (all strings):
    CONFIG              <kind>#<id>                  vehicle / rate / location / route
    VEHICLE#<id>        OBS#<polled_at, UTC ISO>     observation
    VEHICLE#<id>        LEG#<date>#<leg id>          leg (attribute `source` for replace_legs)
    VEHICLE#<id>        NOTE#<date>                  day note
    QUOTA               <UTC date>                   `used` counter, `ttl` (epoch) for cleanup
    VEHICLE#<id>        ANSWER#<segment id>          confirmation answer
    VEHICLE#<id>        OVERRIDE#<leg id>            manual business-km correction
    VEHICLE#<id>        NOTIFIED#<key>               question/day already mailed
    VEHICLE#<id>        REPORT#<YYYY-MM>             what the monthly mail said
    AUTH                TOKEN#<sha256>               single-use login token, `ttl`
    COUNTER             <key>                        rate-limit counter, `ttl`

The record itself is the JSON from codec.py in attribute `data`, so both stores share one
serialisation. Tables are on-demand; queries page through LastEvaluatedKey.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

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

CONFIG_PK = "CONFIG"
QUOTA_PK = "QUOTA"
AUTH_PK = "AUTH"
COUNTER_PK = "COUNTER"
QUOTA_TTL_DAYS = 30


def _vehicle_pk(vehicle: str) -> str:
    return f"VEHICLE#{vehicle}"


def _utc(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


class DynamoDbStore:
    def __init__(self, table_name: str = "kmlog-data", resource: Any = None) -> None:
        if resource is None:
            import boto3

            from ..envfile import aws_region

            resource = boto3.resource("dynamodb", region_name=aws_region())
        self.table = resource.Table(table_name)

    def close(self) -> None:
        pass

    # --- helpers --------------------------------------------------------------------------

    def _query(self, pk: str, sk_from: str | None = None, sk_to: str | None = None, **kw):
        from boto3.dynamodb.conditions import Key

        cond = Key("PK").eq(pk)
        if sk_from is not None and sk_to is not None:
            cond &= Key("SK").between(sk_from, sk_to)
        elif sk_from is not None:
            cond &= Key("SK").begins_with(sk_from)
        args = {"KeyConditionExpression": cond, **kw}
        while True:
            page = self.table.query(**args)
            yield from page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                return
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _delete_all(self, items: Iterable[dict]) -> None:
        with self.table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

    # --- stamdata -------------------------------------------------------------------------

    def _put_config(self, kind: str, id_: str, obj) -> None:
        self.table.put_item(Item={"PK": CONFIG_PK, "SK": f"{kind}#{id_}", "data": dumps(obj)})

    def _config(self, kind: str, cls) -> list:
        return [loads(cls, item["data"]) for item in self._query(CONFIG_PK, f"{kind}#")]

    def _config_one(self, kind: str, cls, id_: str):
        item = self.table.get_item(Key={"PK": CONFIG_PK, "SK": f"{kind}#{id_}"}).get("Item")
        return loads(cls, item["data"]) if item else None

    def put_vehicle(self, vehicle: Vehicle) -> None:
        self._put_config("vehicle", vehicle.vin, vehicle)

    def vehicle(self, vin: str) -> Vehicle | None:
        return self._config_one("vehicle", Vehicle, vin)

    def put_rate(self, rate: Rate) -> None:
        self._put_config("rate", str(rate.year), rate)

    def rate(self, year: int) -> Rate | None:
        return self._config_one("rate", Rate, str(year))

    def rates(self) -> list[Rate]:
        return sorted(self._config("rate", Rate), key=lambda r: r.year)

    def delete_rate(self, year: int) -> None:
        self.table.delete_item(Key={"PK": CONFIG_PK, "SK": f"rate#{year}"})

    def put_settings(self, settings: Settings) -> None:
        self._put_config("settings", "main", settings)

    def settings(self) -> Settings:
        return self._config_one("settings", Settings, "main") or Settings()

    def put_location(self, location: Location) -> None:
        self._put_config("location", location.id, location)

    def locations(self) -> list[Location]:
        return self._config("location", Location)

    def put_route(self, route: Route) -> None:
        self._put_config("route", route.id, route)

    def routes(self) -> list[Route]:
        return self._config("route", Route)

    def replace_places(self, locations: Iterable[Location], routes: Iterable[Route]) -> None:
        old = [*self._query(CONFIG_PK, "location#"), *self._query(CONFIG_PK, "route#")]
        self._delete_all(old)
        with self.table.batch_writer() as batch:
            for loc in locations:
                batch.put_item(
                    Item={"PK": CONFIG_PK, "SK": f"location#{loc.id}", "data": dumps(loc)}
                )
            for r in routes:
                batch.put_item(Item={"PK": CONFIG_PK, "SK": f"route#{r.id}", "data": dumps(r)})

    # --- observations ---------------------------------------------------------------------

    def put_observations(self, observations: Iterable[Observation]) -> int:
        from botocore.exceptions import ClientError

        written = 0
        for o in observations:
            try:
                self.table.put_item(
                    Item={
                        "PK": _vehicle_pk(o.vehicle),
                        "SK": f"OBS#{_utc(o.polled_at)}",
                        "data": dumps(o),
                    },
                    ConditionExpression="attribute_not_exists(SK)",
                )
                written += 1
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
        return written

    def observations(
        self, vehicle: str, start: datetime | None = None, end: datetime | None = None
    ) -> list[Observation]:
        lo = f"OBS#{_utc(start)}" if start else "OBS#"
        hi = f"OBS#{_utc(end)}" if end else "OBS#~"
        items = self._query(_vehicle_pk(vehicle), lo, hi)
        result = [loads(Observation, item["data"]) for item in items]
        if end is not None:  # between() is inclusive; the protocol's end is exclusive
            result = [o for o in result if o.polled_at < end]
        return result

    def observation_before(self, vehicle: str, before: datetime) -> Observation | None:
        items = self._query(
            _vehicle_pk(vehicle), "OBS#", f"OBS#{_utc(before)}", ScanIndexForward=False, Limit=2
        )
        for item in items:  # newest first; between() is inclusive, so skip `before` itself
            obs = loads(Observation, item["data"])
            if obs.polled_at < before:
                return obs
        return None

    # --- legs and day notes ---------------------------------------------------------------

    @staticmethod
    def _leg_item(leg: Leg) -> dict:
        return {
            "PK": _vehicle_pk(leg.vehicle),
            "SK": f"LEG#{leg.date.isoformat()}#{leg.id}",
            "source": leg.source.value,
            "data": dumps(leg),
        }

    def _leg_items(self, vehicle: str, start: date, end: date):
        return self._query(
            _vehicle_pk(vehicle), f"LEG#{start.isoformat()}#", f"LEG#{end.isoformat()}#~"
        )

    def replace_legs(
        self, vehicle: str, source: LegSource, start: date, end: date, legs: Iterable[Leg]
    ) -> None:
        legs = list(legs)
        for leg in legs:
            if leg.vehicle != vehicle or leg.source is not source or not start <= leg.date <= end:
                raise ValueError(f"leg {leg.id} is outside the range being replaced")
        old = [i for i in self._leg_items(vehicle, start, end) if i.get("source") == source.value]
        self._delete_all(old)
        with self.table.batch_writer() as batch:
            for leg in legs:
                batch.put_item(Item=self._leg_item(leg))

    def put_leg(self, leg: Leg) -> None:
        self.table.put_item(Item=self._leg_item(leg))

    def legs(self, vehicle: str, start: date, end: date) -> list[Leg]:
        legs = [loads(Leg, item["data"]) for item in self._leg_items(vehicle, start, end)]
        return sorted(legs, key=lambda leg: (leg.date, leg.id))

    def replace_day_notes(
        self, vehicle: str, start: date, end: date, notes: Iterable[DayNote]
    ) -> None:
        pk = _vehicle_pk(vehicle)
        self._delete_all(self._query(pk, f"NOTE#{start.isoformat()}", f"NOTE#{end.isoformat()}"))
        with self.table.batch_writer() as batch:
            for n in notes:
                batch.put_item(
                    Item={"PK": _vehicle_pk(n.vehicle), "SK": f"NOTE#{n.date}", "data": dumps(n)}
                )

    def day_notes(self, vehicle: str, start: date, end: date) -> list[DayNote]:
        items = self._query(
            _vehicle_pk(vehicle), f"NOTE#{start.isoformat()}", f"NOTE#{end.isoformat()}"
        )
        return [loads(DayNote, item["data"]) for item in items]

    # --- quota ----------------------------------------------------------------------------

    def increment_quota(self, day: date) -> int:
        expires = datetime.combine(day + timedelta(days=QUOTA_TTL_DAYS), time(), UTC)
        result = self.table.update_item(
            Key={"PK": QUOTA_PK, "SK": day.isoformat()},
            UpdateExpression="ADD used :one SET #ttl = :ttl",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":one": 1, ":ttl": int(expires.timestamp())},
            ReturnValues="UPDATED_NEW",
        )
        return int(result["Attributes"]["used"])

    def quota_used(self, day: date) -> int:
        item = self.table.get_item(Key={"PK": QUOTA_PK, "SK": day.isoformat()}).get("Item")
        return int(item["used"]) if item else 0

    def delete_leg(self, leg: Leg) -> None:
        self.table.delete_item(
            Key={"PK": _vehicle_pk(leg.vehicle), "SK": f"LEG#{leg.date.isoformat()}#{leg.id}"}
        )

    # --- confirmations, notifications -----------------------------------------------------

    def _put_meta(self, kind: str, vehicle: str, key: str, data: dict) -> None:
        self.table.put_item(
            Item={"PK": _vehicle_pk(vehicle), "SK": f"{kind}#{key}", "data": json.dumps(data)}
        )

    def _delete_meta(self, kind: str, vehicle: str, key: str) -> None:
        self.table.delete_item(Key={"PK": _vehicle_pk(vehicle), "SK": f"{kind}#{key}"})

    def _meta(self, kind: str, vehicle: str) -> dict[str, dict]:
        prefix = f"{kind}#"
        return {
            item["SK"][len(prefix) :]: json.loads(item["data"])
            for item in self._query(_vehicle_pk(vehicle), prefix)
        }

    def put_sent_report(self, vehicle: str, report: SentReport) -> None:
        data = {
            "sent_at": _utc(report.sent_at),
            "business_km": report.business_km,
            "amount_cents": report.amount_cents,
        }
        self._put_meta("REPORT", vehicle, f"{report.year:04d}-{report.month:02d}", data)

    def sent_report(self, vehicle: str, year: int, month: int) -> SentReport | None:
        data = self._meta("REPORT", vehicle).get(f"{year:04d}-{month:02d}")
        if data is None:
            return None
        sent_at = datetime.fromisoformat(data["sent_at"])
        return SentReport(year, month, sent_at, data["business_km"], data["amount_cents"])

    def put_answer(self, vehicle: str, segment_id: str, answer: str, at: datetime) -> None:
        self._put_meta("ANSWER", vehicle, segment_id, {"answer": answer, "at": _utc(at)})

    def delete_answer(self, vehicle: str, segment_id: str) -> None:
        self._delete_meta("ANSWER", vehicle, segment_id)

    def answers(self, vehicle: str) -> dict[str, str]:
        return {k: v["answer"] for k, v in self._meta("ANSWER", vehicle).items()}

    def put_override(
        self, vehicle: str, leg_id: str, business_km: Decimal, note: str | None
    ) -> None:
        self._put_meta("OVERRIDE", vehicle, leg_id, {"business_km": str(business_km), "note": note})

    def delete_override(self, vehicle: str, leg_id: str) -> None:
        self._delete_meta("OVERRIDE", vehicle, leg_id)

    def overrides(self, vehicle: str) -> dict[str, tuple[Decimal, str | None]]:
        return {
            k: (Decimal(v["business_km"]), v.get("note"))
            for k, v in self._meta("OVERRIDE", vehicle).items()
        }

    def mark_notified(self, vehicle: str, keys: Iterable[str], at: datetime) -> None:
        for key in keys:
            self._put_meta("NOTIFIED", vehicle, key, {"at": _utc(at)})

    def notified(self, vehicle: str) -> set[str]:
        return set(self._meta("NOTIFIED", vehicle))

    # --- web login ------------------------------------------------------------------------

    def put_login_token(self, token_hash: str, expires_at: datetime) -> None:
        self.table.put_item(
            Item={
                "PK": AUTH_PK,
                "SK": f"TOKEN#{token_hash}",
                "expires_at": _utc(expires_at),
                "ttl": int(expires_at.timestamp()),
            }
        )

    def consume_login_token(self, token_hash: str, now: datetime) -> bool:
        """Atomic delete-if-unexpired: a token works exactly once (DynamoDB TTL deletion is
        lazy, so expiry is checked here too)."""
        from botocore.exceptions import ClientError

        try:
            self.table.delete_item(
                Key={"PK": AUTH_PK, "SK": f"TOKEN#{token_hash}"},
                ConditionExpression="attribute_exists(SK) AND expires_at > :now",
                ExpressionAttributeValues={":now": _utc(now)},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def increment_counter(self, key: str, expires_at: datetime) -> int:
        result = self.table.update_item(
            Key={"PK": COUNTER_PK, "SK": key},
            UpdateExpression="ADD used :one SET #ttl = if_not_exists(#ttl, :ttl)",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":one": 1, ":ttl": int(expires_at.timestamp())},
            ReturnValues="UPDATED_NEW",
        )
        return int(result["Attributes"]["used"])
