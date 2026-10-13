"""Linking the car from the web UI (kmlog.carlink + the Auto page)."""

import pytest

import test_web
from kmlog import cardata, carlink
from kmlog.aws import poller
from kmlog.cardata import CarDataClient
from kmlog.store.sqlite import SqliteStore
from kmlog.web.app import WebApp
from kmlog.web.auth import Auth
from test_cardata import NOW, FakeSession, MemoryStore, Resp
from test_web import EVENING, KEY, csrf, event, login

env = test_web.env  # fixture

DEVICE = Resp(
    200,
    {
        "device_code": "dc",
        "user_code": "ABCD-1234",
        "verification_uri": "https://customer.bmwgroup.com/oneid/link",
        "verification_uri_complete": "https://customer.bmwgroup.com/oneid/link?code=ABCD-1234",
        "interval": 5,
        "expires_in": 300,
    },
)
PENDING = Resp(400, {"error": "authorization_pending"})
TOKENS = Resp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600})


def make(tokens: MemoryStore, session: FakeSession) -> CarDataClient:
    """A fresh client per step, like one per web request; the token state is shared."""
    return CarDataClient(tokens, session=session, clock=lambda: NOW)


def test_link_one_primary_vehicle():
    tokens = MemoryStore()
    routes = {
        ("POST", cardata.DEVICE_CODE_URL): [DEVICE],
        ("POST", cardata.TOKEN_URL): [PENDING, TOKENS],
        ("GET", "/customers/vehicles/mappings"): [
            Resp(
                200,
                [
                    {"vin": "WBY1", "mappingType": "PRIMARY"},
                    {"vin": "X", "mappingType": "SECONDARY"},
                ],
            )
        ],
    }
    session = FakeSession(routes)
    carlink.start(make(tokens, session), " cid ", clock=lambda: NOW)
    st = carlink.status(make(tokens, session), clock=lambda: NOW)
    assert st.pending.user_code == "ABCD-1234" and not st.linked and st.client_id == "cid"
    assert carlink.confirm(make(tokens, session), clock=lambda: NOW) == "pending"
    assert carlink.confirm(make(tokens, session), clock=lambda: NOW) == "linked"
    assert tokens.state["vin"] == "WBY1" and tokens.state["refresh_token"] == "rt"
    assert "pending_login" not in tokens.state
    with pytest.raises(carlink.LinkError, match="al een auto"):
        carlink.start(make(tokens, session), "cid")


def test_expired_code_and_several_vehicles():
    tokens = MemoryStore()
    routes = {
        ("POST", cardata.DEVICE_CODE_URL): [DEVICE],
        ("POST", cardata.TOKEN_URL): [TOKENS],
        ("GET", "/customers/vehicles/mappings"): [
            Resp(
                200,
                [{"vin": "A", "mappingType": "PRIMARY"}, {"vin": "B", "mappingType": "PRIMARY"}],
            )
        ],
    }
    session = FakeSession(routes)
    carlink.start(make(tokens, session), "cid", clock=lambda: NOW)
    with pytest.raises(carlink.LinkError, match="verlopen"):
        carlink.confirm(make(tokens, session), clock=lambda: NOW + 301)
    assert carlink.confirm(make(tokens, session), clock=lambda: NOW) == "choose"
    with pytest.raises(carlink.LinkError):
        carlink.choose_vehicle(make(tokens, session), "Z")
    carlink.choose_vehicle(make(tokens, session), "B")
    assert tokens.state["vin"] == "B" and "vehicles" not in tokens.state


def test_unlink_deletes_container_and_keeps_client_id():
    tokens = MemoryStore(
        {"client_id": "cid", "access_token": "at", "refresh_token": "rt", "expires_at": NOW + 3600,
         "vin": "V", "container_id": "C"}
    )  # fmt: skip
    session = FakeSession({("DELETE", "/customers/containers/C"): [Resp(204, "")]})
    carlink.unlink(CarDataClient(tokens, session=session, clock=lambda: NOW))
    assert tokens.state == {"client_id": "cid"}
    assert session.calls[0][0] == "DELETE"


def test_poller_skips_when_no_car_is_linked():
    result = poller.handler({}, None, store=SqliteStore(), token_store=MemoryStore())
    assert result == {"event": "not_linked"}


def test_auto_page_link_flow(monkeypatch):
    store, mails, tokens = SqliteStore(), [], MemoryStore()
    routes = {
        ("POST", cardata.DEVICE_CODE_URL): [DEVICE],
        ("POST", cardata.TOKEN_URL): [PENDING, TOKENS],
        ("GET", "/customers/vehicles/mappings"): [
            Resp(200, [{"vin": "WBY1", "mappingType": "PRIMARY"}])
        ],
        ("DELETE", "/customers/containers/"): [Resp(204, "")],
    }
    session = FakeSession(routes)
    monkeypatch.setattr(cardata.time, "time", lambda: NOW)
    app = WebApp(
        store, Auth(KEY, store), "car", lambda s, b: mails.append((s, b)),
        now=lambda: EVENING,
        cardata=lambda: CarDataClient(
            tokens, session=session, clock=lambda: NOW,
            before_api_call=poller.quota_guard(store, 40, now=lambda: EVENING),
        ),
    )  # fmt: skip
    cookie = login(app, mails)
    token = csrf(app, cookie, "/auto")
    assert "CarData Client ID" in app(event("GET", "/auto", cookie=cookie))["body"]

    def post(path, **form):
        return app(event("POST", path, {"csrf": token, **form}, cookie=cookie))

    assert post("/auto/koppelen", client_id="cid")["headers"]["Location"] == "/auto?ok=code"
    body = app(event("GET", "/auto", cookie=cookie))["body"]
    assert "ABCD-1234" in body and "Ik heb bevestigd" in body
    assert post("/auto/bevestigen")["headers"]["Location"] == "/auto?ok=wacht"
    assert post("/auto/bevestigen")["headers"]["Location"] == "/auto?ok=gekoppeld"
    body = app(event("GET", "/auto", query={"ok": "gekoppeld"}, cookie=cookie))["body"]
    assert "WBY1" in body and "Auto ontkoppelen" in body and "1 / 50" in body  # mappings call
    assert post("/auto/ontkoppelen")["headers"]["Location"] == "/auto?ok=ontkoppeld"
    assert tokens.state == {"client_id": "cid"}


def test_auto_page_without_aws(env):
    app, _, mails, _ = env
    cookie = login(app, mails)
    assert "alleen in de AWS-omgeving" in app(event("GET", "/auto", cookie=cookie))["body"]


def test_vehicle_and_rate_on_stamdata_page(env):
    app, store, mails, _ = env
    cookie = login(app, mails)
    token = csrf(app, cookie)

    def post(path, **form):
        return app(event("POST", path, {"csrf": token, **form}, cookie=cookie))

    post("/stamdata/voertuig", name="BMW i5", plate="xy-987-z")
    assert (store.vehicle("car").name, store.vehicle("car").plate) == ("BMW i5", "XY-987-Z")
    post("/stamdata/tarief", year="2027", euros="0,23")
    assert store.rate(2027).cents_per_km == 23
    assert post("/stamdata/tarief", year="2027", euros="0,235")["statusCode"] == 400
    body = app(event("GET", "/stamdata", cookie=cookie))["body"]
    assert "2026 € 0,25" in body and "2027 € 0,23" in body
    assert 'value="BMW i5"' in body


def test_new_year_uses_the_last_rate():
    from kmlog.model import Rate
    from kmlog.report.service import rate_for

    store = SqliteStore()
    store.put_rate(Rate(2025, 23))
    store.put_rate(Rate(2026, 25))
    assert rate_for(store, 2027) == Rate(2027, 25)
    assert rate_for(store, 2025) == Rate(2025, 23)
    assert rate_for(store, 2024) is None


def test_secret_without_value_means_not_linked():
    class Client:
        class exceptions:
            class ResourceNotFoundException(Exception):
                pass

        def get_secret_value(self, SecretId):
            raise self.exceptions.ResourceNotFoundException()

    assert cardata.SecretsManagerTokenStore("kmlog/cardata", client=Client()).load() == {}
