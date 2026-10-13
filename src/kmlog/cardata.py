"""BMW CarData API client: OAuth device-code flow (PKCE), token refresh, telematicData.

Checked 2026-10-10 against three community clients, BMW's docs and a real car (a BMW i4),
see docs/ONTWERP.md §6:

- auth: device-code flow with PKCE S256; access token 1 hour; refresh token 14 days, rotates
  on every refresh -> the new state is persisted immediately after each refresh.
- quota: 50 REST calls per 24h per account, every API call counts (auth calls do not).
  `before_api_call` is called before each one so the caller can count/limit them.
- data: GET /customers/vehicles/mappings returns a bare list (community docs: {"mappings": ..});
  telematicData needs a container (POST /customers/containers once, then
  GET /customers/vehicles/{vin}/telematicData?containerId=...); each descriptor maps to
  {"value": <string>, "unit": ..., "timestamp": ...}.

Token state is a dict kept in a TokenStore: a JSON file locally (.cardata_tokens.json) or the
Secrets Manager secret `kmlog/cardata` in AWS. Keys: client_id, access_token, refresh_token,
expires_at (epoch seconds), container_id, container_descriptors, vin, plus while linking via
the web UI: pending_login (a started device-code login) and vehicles (the account's mappings).
A container whose descriptors differ from DESCRIPTORS is replaced (and the old one deleted) on
the next poll.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import requests

from .model import Observation

DEVICE_CODE_URL = "https://customer.bmwgroup.com/gcdm/oauth/device/code"
TOKEN_URL = "https://customer.bmwgroup.com/gcdm/oauth/token"
API_BASE = "https://api-cardata.bmwgroup.com"
# Only the REST API: asking for cardata:streaming:read when the client is not subscribed to
# "CarData Stream" makes BMW answer the device-code request with a bare 400 invalid_request.
DEFAULT_SCOPES = "authenticate_user openid cardata:api:read"

ODOMETER_DESCRIPTOR = "vehicle.vehicle.travelledDistance"
LATITUDE_DESCRIPTOR = "vehicle.cabin.infotainment.navigation.currentLocation.latitude"
LONGITUDE_DESCRIPTOR = "vehicle.cabin.infotainment.navigation.currentLocation.longitude"
# vehicle.isMoving and engine.isActive/isIgnitionOn were tried 2026-10-12/13: a BMW i4 never
# fills them (null value and timestamp, also mid-drive), so they are not requested (§11).
DESCRIPTORS = [
    ODOMETER_DESCRIPTOR,
    LATITUDE_DESCRIPTOR,
    LONGITUDE_DESCRIPTOR,
]
CONTAINER_NAME = "kmlog"

SECRET_KEYS = (
    "client_id",
    "access_token",
    "refresh_token",
    "expires_at",
    "container_id",
    "container_descriptors",
    "vin",
    "pending_login",
    "vehicles",
)
DEFAULT_SECRET_ID = "kmlog/cardata"


class CarDataError(RuntimeError):
    pass


class QuotaExceeded(CarDataError):
    """Our own daily limit (below BMW's 50 calls/24h) was reached; raised by before_api_call."""


class AuthError(CarDataError):
    """No usable token: refresh failed or never logged in -> re-run the device-code login."""


class ApiError(CarDataError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path}: {status} {body}")
        self.status = status
        self.body = body


# --- token state --------------------------------------------------------------------------


class TokenStore(Protocol):
    def load(self) -> dict: ...
    def save(self, state: dict) -> None: ...


def normalise_state(state: dict) -> dict:
    """Accept the probe's older file format (obtained_at + expires_in) as well."""
    state = dict(state)
    if "expires_at" not in state and "access_token" in state:
        state["expires_at"] = state.get("obtained_at", 0) + state.get("expires_in", 3600)
    return state


class FileTokenStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        return normalise_state(json.loads(self.path.read_text()))

    def save(self, state: dict) -> None:
        self.path.write_text(json.dumps(state, indent=2))
        self.path.chmod(0o600)


class SecretsManagerTokenStore:
    def __init__(self, secret_id: str = DEFAULT_SECRET_ID, client: Any = None) -> None:
        if client is None:
            import boto3

            from .envfile import aws_region

            client = boto3.client("secretsmanager", region_name=aws_region())
        self.secret_id = secret_id
        self.client = client

    def load(self) -> dict:
        try:
            value = self.client.get_secret_value(SecretId=self.secret_id)["SecretString"]
        except self.client.exceptions.ResourceNotFoundException:
            return {}  # created by Terraform without a value: no car linked yet
        return normalise_state(json.loads(value) if value.strip() else {})

    def save(self, state: dict) -> None:
        payload = {k: state[k] for k in SECRET_KEYS if state.get(k) is not None}
        self.client.put_secret_value(SecretId=self.secret_id, SecretString=json.dumps(payload))


# --- client -------------------------------------------------------------------------------


@dataclass
class DeviceLogin:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int
    code_verifier: str
    expires_at: float = 0.0  # epoch seconds; set when the login is kept between web requests

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> DeviceLogin:
        return cls(**{f.name: data[f.name] for f in dataclasses.fields(cls) if f.name in data})


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


class CarDataClient:
    def __init__(
        self,
        store: TokenStore,
        client_id: str | None = None,
        *,
        scopes: str = DEFAULT_SCOPES,
        device_code_url: str = DEVICE_CODE_URL,
        token_url: str = TOKEN_URL,
        api_base: str = API_BASE,
        session: requests.Session | None = None,
        before_api_call: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.time,
        timeout: float = 30,
    ) -> None:
        self.store = store
        self.state = store.load()
        if client_id:
            self.state["client_id"] = client_id
        self.scopes = scopes
        self.device_code_url = device_code_url
        self.token_url = token_url
        self.api_base = api_base.rstrip("/")
        self.http = session or requests.Session()
        self.before_api_call = before_api_call
        self.clock = clock
        self.timeout = timeout

    @property
    def client_id(self) -> str:
        client_id = self.state.get("client_id")
        if not client_id:
            raise AuthError("no BMW client_id configured")
        return client_id

    def _save(self) -> None:
        self.store.save(self.state)

    def _apply_tokens(self, tokens: dict) -> None:
        self.state.update(
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token", self.state.get("refresh_token")),
            expires_at=self.clock() + int(tokens.get("expires_in", 3600)),
        )
        self._save()

    # auth

    def start_device_login(self) -> DeviceLogin:
        verifier, challenge = _pkce_pair()
        resp = self.http.post(
            self.device_code_url,
            data={
                "client_id": self.client_id,
                "scope": self.scopes,
                "response_type": "device_code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            timeout=self.timeout,
        )
        if not resp.ok:
            raise AuthError(
                f"device-code request failed: {resp.status_code} {resp.text} "
                "(check the client id and that the client is subscribed to every scope)"
            )
        dc = resp.json()
        return DeviceLogin(
            device_code=dc["device_code"],
            user_code=dc["user_code"],
            verification_uri=dc.get("verification_uri_complete") or dc["verification_uri"],
            interval=int(dc.get("interval", 5)),
            expires_in=int(dc.get("expires_in", 300)),
            code_verifier=verifier,
        )

    def poll_device_login(self, login: DeviceLogin) -> str:
        """One token request: "ok" (tokens stored), "pending" or "slow_down"."""
        resp = self.http.post(
            self.token_url,
            data={
                "client_id": self.client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": login.device_code,
                "code_verifier": login.code_verifier,
            },
            timeout=self.timeout,
        )
        if resp.ok:
            self._apply_tokens(resp.json())
            return "ok"
        error = resp.json().get("error", "")
        if error == "authorization_pending":
            return "pending"
        if error == "slow_down":
            return "slow_down"
        raise AuthError(f"token request failed: {resp.status_code} {resp.text}")

    def complete_device_login(
        self, login: DeviceLogin, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        interval = login.interval
        waited = 0
        while waited < login.expires_in:
            sleep(interval)
            waited += interval
            result = self.poll_device_login(login)
            if result == "ok":
                return
            if result == "slow_down":
                interval += 5
        raise AuthError("device code expired before it was authorised")

    @property
    def linked(self) -> bool:
        return bool(self.state.get("refresh_token"))

    def refresh(self) -> None:
        if not self.state.get("refresh_token"):
            raise AuthError("no refresh token; log in with the device-code flow first")
        resp = self.http.post(
            self.token_url,
            data={
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": self.state["refresh_token"],
            },
            timeout=self.timeout,
        )
        if not resp.ok:
            raise AuthError(f"token refresh failed: {resp.status_code} {resp.text}")
        self._apply_tokens(resp.json())

    def ensure_token(self, min_valid_s: int = 300) -> None:
        if not self.state.get("access_token"):
            raise AuthError("not logged in; run the device-code flow first")
        if self.clock() > self.state.get("expires_at", 0) - min_valid_s:
            self.refresh()

    # API

    def request(self, method: str, path: str, **kwargs) -> Any:
        self.ensure_token()
        if self.before_api_call:
            self.before_api_call(method, path)
        resp = self.http.request(
            method,
            f"{self.api_base}{path}",
            headers={
                "Authorization": f"Bearer {self.state['access_token']}",
                "Accept": "application/json",
                "x-version": "v1",
            },
            timeout=self.timeout,
            **kwargs,
        )
        if not resp.ok:
            # 403 CU-103: scope, CU-104: VIN not yours/not PRIMARY, CU-105: bad containerId,
            # CU-429 / 429: quota
            raise ApiError(method, path, resp.status_code, resp.text)
        return resp.json() if resp.text else None  # DELETE answers 204 without a body

    def mappings(self) -> list[dict]:
        response = self.request("GET", "/customers/vehicles/mappings")
        return response.get("mappings", []) if isinstance(response, dict) else response

    def vin(self) -> str:
        if not self.state.get("vin"):
            primary = [m["vin"] for m in self.mappings() if m.get("mappingType") == "PRIMARY"]
            if len(primary) != 1:
                raise CarDataError(f"expected exactly one PRIMARY vehicle, got {primary}")
            self.state["vin"] = primary[0]
            self._save()
        return self.state["vin"]

    def container_id(self) -> str:
        current = self.state.get("container_id")
        outdated = current if self.state.get("container_descriptors") != DESCRIPTORS else None
        if not current or outdated:
            container = self.request(
                "POST",
                "/customers/containers",
                json={
                    "name": CONTAINER_NAME,
                    "purpose": "kilometeradministratie",
                    "technicalDescriptors": DESCRIPTORS,
                },
            )
            self.state["container_id"] = container["containerId"]
            self.state["container_descriptors"] = DESCRIPTORS
            self._save()
            if outdated:
                # max 10 containers per account; if this fails the old one just lingers
                try:
                    self.request("DELETE", f"/customers/containers/{outdated}")
                except CarDataError:
                    pass
        return self.state["container_id"]

    def telematic_data(self) -> dict:
        vin = self.vin()
        container_id = self.container_id()
        data = self.request(
            "GET",
            f"/customers/vehicles/{vin}/telematicData",
            params={"containerId": container_id},
        )
        return data.get("telematicData", data)

    def observe(self, vehicle: str, polled_at: datetime | None = None) -> Observation:
        polled_at = polled_at or datetime.now(UTC)
        telematic = self.telematic_data()
        return parse_observation(telematic, vehicle, polled_at)


# --- parsing ------------------------------------------------------------------------------


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _float(entry: dict | None) -> float | None:
    if not entry or entry.get("value") in (None, ""):
        return None
    return float(entry["value"])


def parse_observation(telematic: dict, vehicle: str, polled_at: datetime) -> Observation:
    odo = telematic.get(ODOMETER_DESCRIPTOR)
    if not odo or odo.get("value") in (None, ""):
        raise CarDataError(f"no {ODOMETER_DESCRIPTOR} in telematicData")
    odometer = Decimal(str(odo["value"]))
    lat_entry = telematic.get(LATITUDE_DESCRIPTOR)
    return Observation(
        vehicle=vehicle,
        polled_at=polled_at.astimezone(UTC),
        odometer_km=int(odometer),
        odometer_ts=_timestamp(odo.get("timestamp")) or polled_at.astimezone(UTC),
        lat=_float(lat_entry),
        lon=_float(telematic.get(LONGITUDE_DESCRIPTOR)),
        loc_ts=_timestamp((lat_entry or {}).get("timestamp")),
        raw=telematic,
    )
