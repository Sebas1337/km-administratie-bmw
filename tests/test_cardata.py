import base64
import hashlib
import json
from datetime import UTC, datetime

import boto3
import pytest

from kmlog import cardata
from kmlog.cardata import (
    ApiError,
    AuthError,
    CarDataClient,
    FileTokenStore,
    SecretsManagerTokenStore,
    parse_observation,
)

# shape of a real response (2026-10-10)
TELEMATIC = {
    "telematicData": {
        cardata.LATITUDE_DESCRIPTOR: {
            "timestamp": "2026-10-10T14:01:06.000Z",
            "unit": "degrees",
            "value": "52.08940277777778",
        },
        cardata.ODOMETER_DESCRIPTOR: {
            "timestamp": "2026-10-10T14:01:06.000Z",
            "unit": "km",
            "value": "23456",
        },
        cardata.LONGITUDE_DESCRIPTOR: {
            "timestamp": "2026-10-10T14:01:06.000Z",
            "unit": "degrees",
            "value": "5.110025000000001",
        },
    }
}
NOW = 1_760_000_000.0


class Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """Scripted responses per (method, url-suffix); records every call."""

    def __init__(self, routes: dict[tuple[str, str], list[Resp]]):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for (m, suffix), responses in self.routes.items():
            if m == method and url.endswith(suffix):
                return responses.pop(0) if len(responses) > 1 else responses[0]
        raise AssertionError(f"unexpected {method} {url}")

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


class MemoryStore:
    def __init__(self, state=None):
        self.state = dict(state or {})
        self.saves = 0

    def load(self):
        return dict(self.state)

    def save(self, state):
        self.state = dict(state)
        self.saves += 1


def logged_in(**extra):
    return MemoryStore(
        {
            "client_id": "cid",
            "access_token": "at",
            "refresh_token": "rt",
            "expires_at": NOW + 3600,
            **extra,
        }
    )


def client(store, routes, calls=None):
    return CarDataClient(
        store,
        session=FakeSession(routes),
        clock=lambda: NOW,
        before_api_call=(lambda m, p: calls.append((m, p))) if calls is not None else None,
    )


def test_parse_observation_matches_real_response():
    polled = datetime(2026, 10, 10, 16, 13, tzinfo=UTC)
    obs = parse_observation(TELEMATIC["telematicData"], "car", polled)
    assert obs.odometer_km == 23456
    assert obs.odometer_ts == datetime(2026, 10, 10, 14, 1, 6, tzinfo=UTC)
    assert (obs.lat, obs.lon) == (52.08940277777778, 5.110025000000001)
    assert obs.loc_ts == obs.odometer_ts
    assert obs.polled_at == polled


def test_parse_without_position_and_with_decimal_odometer():
    tele = {cardata.ODOMETER_DESCRIPTOR: {"value": "23456.0", "timestamp": "2026-10-10T14:01:06Z"}}
    obs = parse_observation(tele, "car", datetime(2026, 10, 10, tzinfo=UTC))
    assert (obs.odometer_km, obs.lat, obs.lon, obs.loc_ts) == (23456, None, None, None)


def test_parse_without_odometer_fails():
    with pytest.raises(cardata.CarDataError):
        parse_observation({}, "car", datetime(2026, 10, 10, tzinfo=UTC))


def test_first_observe_resolves_vin_and_container_once():
    store, calls = logged_in(), []
    c = client(
        store,
        {
            ("GET", "/customers/vehicles/mappings"): [
                Resp(200, [{"vin": "WBY1", "mappingType": "PRIMARY"}])
            ],
            ("POST", "/customers/containers"): [Resp(201, {"containerId": "E00O050G311QH"})],
            ("GET", "/customers/vehicles/WBY1/telematicData"): [Resp(200, TELEMATIC)],
        },
        calls,
    )
    obs = c.observe("car")
    assert obs.odometer_km == 23456
    assert (store.state["vin"], store.state["container_id"]) == ("WBY1", "E00O050G311QH")
    c.observe("car")
    assert [p for _, p in calls] == [
        "/customers/vehicles/mappings",
        "/customers/containers",
        "/customers/vehicles/WBY1/telematicData",
        "/customers/vehicles/WBY1/telematicData",
    ]
    _, _, kwargs = c.http.calls[-1]
    assert kwargs["params"] == {"containerId": "E00O050G311QH"}
    assert kwargs["headers"]["x-version"] == "v1"


def test_mappings_wrapped_in_object_also_work():
    c = client(
        logged_in(),
        {
            ("GET", "/mappings"): [
                Resp(200, {"mappings": [{"vin": "A", "mappingType": "SECONDARY"}]})
            ]
        },
    )
    with pytest.raises(cardata.CarDataError, match="PRIMARY"):
        c.vin()


def test_expiring_token_is_refreshed_and_rotation_persisted_before_the_call():
    store = logged_in(
        expires_at=NOW + 60, vin="V", container_id="C", container_descriptors=cardata.DESCRIPTORS
    )
    c = client(
        store,
        {
            ("POST", cardata.TOKEN_URL): [
                Resp(200, {"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600})
            ],
            ("GET", "/customers/vehicles/V/telematicData"): [Resp(200, TELEMATIC)],
        },
    )
    c.observe("car")
    assert store.state["access_token"] == "at2"
    assert store.state["refresh_token"] == "rt2"
    assert store.state["expires_at"] == NOW + 3600
    method, url, kwargs = c.http.calls[0]
    assert url == cardata.TOKEN_URL
    assert kwargs["data"]["grant_type"] == "refresh_token"
    assert kwargs["data"]["refresh_token"] == "rt"
    assert c.http.calls[1][2]["headers"]["Authorization"] == "Bearer at2"


def test_failed_refresh_raises_auth_error():
    c = client(
        logged_in(expires_at=NOW - 1),
        {("POST", cardata.TOKEN_URL): [Resp(400, {"error": "invalid_grant"})]},
    )
    with pytest.raises(AuthError):
        c.observe("car")


def test_not_logged_in():
    with pytest.raises(AuthError):
        client(MemoryStore({"client_id": "cid"}), {}).observe("car")


def test_api_error_carries_status():
    c = client(
        logged_in(vin="V", container_id="C", container_descriptors=cardata.DESCRIPTORS),
        {("GET", "/telematicData"): [Resp(403, {"exveErrorId": "CU-429"})]},
    )
    with pytest.raises(ApiError) as exc:
        c.observe("car")
    assert exc.value.status == 403
    assert "CU-429" in exc.value.body


def test_quota_hook_can_stop_the_call():
    class Stop(Exception):
        pass

    def hook(method, path):
        raise Stop

    c = CarDataClient(
        logged_in(vin="V", container_id="C", container_descriptors=cardata.DESCRIPTORS),
        session=FakeSession({}),
        clock=lambda: NOW,
        before_api_call=hook,
    )
    with pytest.raises(Stop):
        c.observe("car")
    assert c.http.calls == []


def test_device_login_uses_pkce():
    store = MemoryStore({"client_id": "cid"})
    c = client(
        store,
        {
            ("POST", cardata.DEVICE_CODE_URL): [
                Resp(
                    200,
                    {
                        "device_code": "dc",
                        "user_code": "ABCD",
                        "verification_uri": "https://example/verify",
                        "interval": 5,
                        "expires_in": 300,
                    },
                )
            ],
            ("POST", cardata.TOKEN_URL): [
                Resp(400, {"error": "authorization_pending"}),
                Resp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}),
            ],
        },
    )
    login = c.start_device_login()
    c.complete_device_login(login, sleep=lambda s: None)
    dc_call = c.http.calls[0][2]["data"]
    assert dc_call["response_type"] == "device_code"
    assert dc_call["code_challenge_method"] == "S256"
    assert dc_call["scope"] == "authenticate_user openid cardata:api:read"
    expected = base64.urlsafe_b64encode(hashlib.sha256(login.code_verifier.encode()).digest())
    assert dc_call["code_challenge"] == expected.rstrip(b"=").decode()
    assert c.http.calls[-1][2]["data"]["code_verifier"] == login.code_verifier
    assert store.state["access_token"] == "at"


def test_file_store_reads_the_probe_format(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"access_token": "a", "obtained_at": 100.0, "expires_in": 3599}))
    state = FileTokenStore(path).load()
    assert state["expires_at"] == 3699.0
    FileTokenStore(path).save(state)
    assert path.stat().st_mode & 0o777 == 0o600


def test_secrets_manager_store_roundtrip(aws):
    sm = boto3.client("secretsmanager")
    sm.create_secret(Name="kmlog/cardata")
    store = SecretsManagerTokenStore("kmlog/cardata", sm)
    store.save({"client_id": "cid", "access_token": "a", "expires_at": 1.0, "id_token": "x"})
    assert store.load() == {"client_id": "cid", "access_token": "a", "expires_at": 1.0}


def test_container_with_old_descriptors_is_replaced_once():
    store, calls = logged_in(vin="V", container_id="OLD"), []
    c = client(
        store,
        {
            ("POST", "/customers/containers"): [Resp(201, {"containerId": "NEW"})],
            ("DELETE", "/customers/containers/OLD"): [Resp(204, "")],
            ("GET", "/customers/vehicles/V/telematicData"): [Resp(200, TELEMATIC)],
        },
        calls,
    )
    c.observe("car")
    c.observe("car")
    assert store.state["container_id"] == "NEW"
    assert store.state["container_descriptors"] == cardata.DESCRIPTORS
    assert calls == [
        ("POST", "/customers/containers"),
        ("DELETE", "/customers/containers/OLD"),
        ("GET", "/customers/vehicles/V/telematicData"),
        ("GET", "/customers/vehicles/V/telematicData"),
    ]
    assert c.http.calls[0][2]["json"]["technicalDescriptors"] == cardata.DESCRIPTORS
    assert c.http.calls[2][2]["params"] == {"containerId": "NEW"}


def test_failed_delete_of_old_container_does_not_stop_the_poll():
    store = logged_in(vin="V", container_id="OLD")
    c = client(
        store,
        {
            ("POST", "/customers/containers"): [Resp(201, {"containerId": "NEW"})],
            ("DELETE", "/customers/containers/OLD"): [Resp(404, {"error": "gone"})],
            ("GET", "/customers/vehicles/V/telematicData"): [Resp(200, TELEMATIC)],
        },
    )
    assert c.observe("car").odometer_km == 23456
    assert store.state["container_id"] == "NEW"
