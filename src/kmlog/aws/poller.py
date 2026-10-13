"""Hourly poller: CarData telematicData -> Observation -> re-derived legs (ONTWERP.md §5).

Invoked hourly by EventBridge Scheduler (default 06-23 Amsterdam, see infra). Order of business:
1. Load the token state from Secrets Manager; refresh when the access token expires within
   5 minutes. Refresh tokens rotate, so the new state is written back immediately.
2. Count every API call against a per-UTC-day quota in DynamoDB; refuse beyond QUOTA_LIMIT.
3. Fetch telematicData, store the Observation (idempotent per polled_at).
4. Re-derive + classify the CarData legs of yesterday and today.

Errors (refresh failure, API errors) are raised so the Lambda Errors metric and its alarm fire.
Hitting the quota is logged, not raised: retrying would not help until 00:00 UTC. So is "no car
linked yet" (link it on the Auto page of the web UI) and "choose a vehicle first".
"""

from __future__ import annotations

from datetime import UTC, datetime

from .. import cardata
from ..cardata import QuotaExceeded
from ..derive import match_location
from ..process import rederive
from ..store import Store
from . import env, log_event


def quota_guard(store: Store, limit: int, now=lambda: datetime.now(UTC)):
    def before_api_call(method: str, path: str) -> None:
        used = store.increment_quota(now().date())
        if used > limit:
            raise QuotaExceeded(f"CarData quota: {used} > {limit} calls on {now().date()} UTC")

    return before_api_call


def poll(store: Store, client: cardata.CarDataClient, vehicle: str, now: datetime) -> dict:
    obs = client.observe(vehicle, polled_at=now)
    new = store.put_observations([obs])
    legs = rederive(store, vehicle, now)
    loc = match_location(obs.lat, obs.lon, store.locations())
    return {
        "event": "poll",
        "vehicle": vehicle,
        "odometer_km": obs.odometer_km,
        "odometer_ts": obs.odometer_ts.isoformat(),
        "location": loc.id if loc else ("unknown" if obs.lat is not None else None),
        "new_observation": bool(new),
        "legs_recent": len(legs),
        "business_km_recent": sum(int(leg.business_km) for leg in legs),
        "quota_used": store.quota_used(now.date()),
    }


def handler(event, context, *, store: Store | None = None, token_store=None) -> dict:
    from ..store.dynamodb import DynamoDbStore

    store = store or DynamoDbStore(env("TABLE_NAME", "kmlog-data"))
    token_store = token_store or cardata.SecretsManagerTokenStore(env("SECRET_ID", "kmlog/cardata"))
    now = datetime.now(UTC)
    client = cardata.CarDataClient(
        token_store, before_api_call=quota_guard(store, int(env("QUOTA_LIMIT", "40")))
    )
    if not client.linked:
        result = {"event": "not_linked"}
    elif not client.state.get("vin") and client.state.get("vehicles"):
        result = {"event": "vehicle_not_chosen"}
    else:
        result = None
    try:
        result = result or poll(store, client, env("VEHICLE_ID", "car"), now)
    except QuotaExceeded as e:
        result = {"event": "quota_exceeded", "message": str(e)}
    log_event(**result)
    return result
