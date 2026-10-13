"""Web UI: login, sessions, CSRF, pages and answers via fake Function URL events."""

import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

import pytest

from kmlog.aws import web as web_handler
from kmlog.derive import AMSTERDAM
from kmlog.model import LegSource, LegStatus, LocationType, Observation
from kmlog.process import rederive
from kmlog.store.sqlite import SqliteStore
from kmlog.web.app import Request, WebApp, safe_next
from kmlog.web.auth import Auth
from test_aws_handlers import seeded
from test_maandstaat import manual
from test_voorbeelddagen import UNKNOWN, WORKDAY, normal_day, observations

HOST = "abc123.lambda-url.eu-central-1.on.aws"
KEY = b"k" * 32
EVENING = datetime(2026, 10, 7, 21, 30, tzinfo=UTC)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def env():
    store = seeded(SqliteStore())
    store.put_observations(observations(WORKDAY, normal_day(96)))  # day G: one open question
    rederive(store, "car", EVENING)
    mails = []
    clock = Clock(EVENING)
    app = WebApp(store, Auth(KEY, store), "car", lambda s, b: mails.append((s, b)), now=clock)
    return app, store, mails, clock


def event(method, path, form=None, cookie=None, query=None, origin=None):
    headers = {"host": HOST}
    if form is not None:
        headers["content-type"] = "application/x-www-form-urlencoded"
    if origin:
        headers["origin"] = origin
    return {
        "version": "2.0",
        "rawPath": path,
        "queryStringParameters": query,
        "headers": headers,
        "cookies": [cookie] if cookie else None,
        "requestContext": {"http": {"method": method}},
        "body": urlencode(form) if form is not None else None,
        "isBase64Encoded": False,
    }


def login(app, mails) -> str:
    """Full magic-link flow; returns the session cookie ('name=value')."""
    app(event("POST", "/login", {"next": "/"}))
    link = re.search(r"https://\S+", mails[-1][1]).group(0)
    assert link.startswith(f"https://{HOST}/login/bevestig?")
    query = dict(p.split("=", 1) for p in link.split("?", 1)[1].split("&"))
    page = app(event("GET", "/login/bevestig", query=query))
    assert page["statusCode"] == 200 and "Inloggen op dit apparaat" in page["body"]
    resp = app(event("POST", "/login/bevestig", {"t": query["t"], "next": "/"}))
    assert resp["statusCode"] == 303
    cookie = resp["cookies"][0]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
    return cookie.split(";")[0]


def csrf(app, cookie, path="/stamdata") -> str:
    body = app(event("GET", path, cookie=cookie))["body"]
    return re.search(r'name="csrf" value="([^"]+)"', body).group(1)


# --- auth -----------------------------------------------------------------------------------


def test_pages_require_login(env):
    app, *_ = env
    resp = app(event("GET", "/maand/2026-10"))
    assert resp["statusCode"] == 303
    assert resp["headers"]["Location"] == "/login?next=%2Fmaand%2F2026-10"
    assert app(event("POST", "/antwoord", {"segment": "x"}))["statusCode"] == 401


def test_magic_link_is_single_use_and_expires(env):
    app, store, mails, clock = env
    login(app, mails)
    token = re.search(r"t=([^&\s]+)", mails[-1][1]).group(1)
    reuse = app(event("POST", "/login/bevestig", {"t": token}))
    assert reuse["statusCode"] == 400 and not reuse["cookies"]

    app(event("POST", "/login", {}))
    token = re.search(r"t=([^&\s]+)", mails[-1][1]).group(1)
    clock.now = EVENING + timedelta(minutes=16)
    assert app(event("POST", "/login/bevestig", {"t": token}))["statusCode"] == 400


def test_login_mail_is_rate_limited(env):
    app, store, mails, clock = env
    for _ in range(5):
        assert app(event("POST", "/login", {}))["statusCode"] == 200
    assert app(event("POST", "/login", {}))["statusCode"] == 429
    assert len(mails) == 5
    clock.now = EVENING + timedelta(hours=1)
    assert app(event("POST", "/login", {}))["statusCode"] == 200


def test_tampered_or_expired_cookie_is_rejected(env):
    app, store, mails, clock = env
    cookie = login(app, mails)
    assert app(event("GET", "/stamdata", cookie=cookie))["statusCode"] == 200
    name, value = cookie.split("=", 1)
    payload, sig = value.split(".")
    forged = f"{name}={payload[:-1]}A.{sig}"
    assert app(event("GET", "/stamdata", cookie=forged))["statusCode"] == 303
    other_key = Auth(b"x" * 32, store).new_session_cookie(EVENING).split(";")[0]
    assert app(event("GET", "/stamdata", cookie=other_key))["statusCode"] == 303
    clock.now = EVENING + timedelta(days=31)
    assert app(event("GET", "/stamdata", cookie=cookie))["statusCode"] == 303


def test_post_without_valid_csrf_or_from_other_origin_is_rejected(env):
    app, store, mails, _ = env
    cookie = login(app, mails)
    form = {"name": "X", "type": "business", "lat": "52.1", "lon": "5.1"}
    assert app(event("POST", "/stamdata/locatie", form, cookie))["statusCode"] == 403
    bad = {**form, "csrf": "nope"}
    assert app(event("POST", "/stamdata/locatie", bad, cookie))["statusCode"] == 403
    good = {**form, "csrf": csrf(app, cookie)}
    evil = event("POST", "/stamdata/locatie", good, cookie, origin="https://evil.example")
    assert app(evil)["statusCode"] == 403
    opaque = event("POST", "/stamdata/locatie", good, cookie, origin="null")
    assert app(opaque)["statusCode"] == 403
    assert len(store.locations()) == 2
    ok = event("POST", "/stamdata/locatie", good, cookie, origin=f"https://{HOST}")
    assert app(ok)["statusCode"] == 303
    assert len(store.locations()) == 3


def test_logout_clears_cookie(env):
    app, store, mails, _ = env
    cookie = login(app, mails)
    resp = app(event("POST", "/uitloggen", {"csrf": csrf(app, cookie)}, cookie))
    assert resp["statusCode"] == 303 and "Max-Age=0" in resp["cookies"][0]


def test_safe_next():
    assert safe_next("/dag/2026-10-07") == "/dag/2026-10-07"
    for bad in ("https://evil.example", "//evil.example", "/\\evil", None, ""):
        assert safe_next(bad) == "/"


def test_security_headers(env):
    app, *_ = env
    response = app(event("GET", "/login"))
    headers = response["headers"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "script-src" not in headers["Content-Security-Policy"]  # default-src 'none'
    # no-referrer would make browsers send "Origin: null" on every form post (all 403);
    # a <meta name="referrer"> in the page overrides the header, so it must not be there either
    assert headers["Referrer-Policy"] == "same-origin"
    assert 'name="referrer"' not in response["body"]


# --- pages and actions ----------------------------------------------------------------------


def test_month_page_shows_trips_and_open_question(env):
    app, store, mails, _ = env
    store.put_leg(manual(date(2026, 10, 5), 140))
    cookie = login(app, mails)
    body = app(event("GET", "/maand/2026-10", cookie=cookie))["body"]
    assert "Oktober 2026" in body
    assert "Openstaande vragen (1)" in body
    assert "Thuis → Klant → Thuis" in body
    assert "Omweg (alle km zakelijk)" in body
    assert "€ 70,25" in body  # 140 + 141 km x 0.25
    assert '<td class="n">40.167</td>' in body  # km-stand at the end of day G
    # "Auto nu": latest reading (day G ends at home on 40.167) with a static OSM map
    assert "Auto nu" in body and "40.167 km" in body and "Thuis" in body
    assert "https://tile.openstreetmap.org/15/" in body and 'referrerpolicy="origin"' in body
    resp = app(event("GET", "/maand/2026-10", cookie=cookie))
    assert "https://tile.openstreetmap.org" in resp["headers"]["Content-Security-Policy"]
    assert "Auto nu" not in app(event("GET", "/maand/2026-09", cookie=cookie))["body"]


def test_month_page_per_trip(env):
    app, store, mails, _ = env
    store.put_leg(manual(date(2026, 10, 5), 140, "Leverancier"))
    cookie = login(app, mails)
    per_day = app(event("GET", "/maand/2026-10", cookie=cookie))["body"]
    assert '<a href="/maand/2026-10?weergave=ritten">Per rit</a>' in per_day
    body = app(event("GET", "/maand/2026-10", query={"weergave": "ritten"}, cookie=cookie))["body"]
    assert "<strong>Per rit</strong>" in body
    assert "Thuis → Klant" in body and "Klant → Thuis" in body
    assert "Thuis → Klant → Thuis" not in body  # one row per rit, not per day
    assert "na 07:00 → 08:30" in body  # day G: last poll at home 07:00, arrived 08:30
    assert "40.000 → 40.071" in body
    assert '<th class="n">Km-stand eind</th>' in per_day
    assert "Leverancier" in body  # manual trip: its note is the description
    assert "€ 70,25" in body  # same total as per day


def test_answer_question_via_post(env):
    app, store, mails, _ = env
    cookie = login(app, mails)
    seg = next(leg.segment_id for leg in store.legs("car", WORKDAY, WORKDAY) if leg.question)
    form = {"csrf": csrf(app, cookie), "segment": seg, "answer": "detour", "next": "/maand/2026-10"}
    resp = app(event("POST", "/antwoord", form, cookie))
    assert resp["statusCode"] == 303
    assert resp["headers"]["Location"] == "/maand/2026-10?ok=antwoord"
    legs = store.legs("car", WORKDAY, WORKDAY)
    assert int(sum(leg.business_km for leg in legs)) == 167
    assert all(leg.status is not LegStatus.NEEDS_CONFIRMATION for leg in legs)
    rederive(store, "car", EVENING)  # next hourly poll
    assert int(sum(leg.business_km for leg in store.legs("car", WORKDAY, WORKDAY))) == 167
    body = app(event("GET", "/maand/2026-10", query={"ok": "antwoord"}, cookie=cookie))["body"]
    assert "Antwoord opgeslagen." in body and "Openstaande vragen" not in body


def test_invalid_answer_is_a_400(env):
    app, store, mails, _ = env
    cookie = login(app, mails)
    seg = next(leg.segment_id for leg in store.legs("car", WORKDAY, WORKDAY) if leg.question)
    form = {"csrf": csrf(app, cookie), "segment": seg, "answer": "confirm"}
    resp = app(event("POST", "/antwoord", form, cookie))
    assert resp["statusCode"] == 400 and "past niet" in resp["body"]


def test_day_page_correction_and_manual_trip(env):
    app, store, mails, _ = env
    cookie = login(app, mails)
    body = app(event("GET", f"/dag/{WORKDAY}", cookie=cookie))["body"]
    assert "openstreetmap.org" in body and "vertrokken na" in body
    assert "km-stand 40.000 → 40.071" in body  # per leg
    assert "Metingen" in body and "40.167" in body and "Thuis" in body  # hourly readings
    token = csrf(app, cookie)
    leg = store.legs("car", WORKDAY, WORKDAY)[1]
    form = {"csrf": token, "leg": leg.id, "date": str(WORKDAY), "business_km": "90", "note": "file"}
    assert app(event("POST", "/correctie", form, cookie))["statusCode"] == 303
    assert int(store.legs("car", WORKDAY, WORKDAY)[1].business_km) == 90
    too_much = {**form, "business_km": "97"}
    assert app(event("POST", "/correctie", too_much, cookie))["statusCode"] == 400

    form = {"csrf": token, "date": "2026-10-09", "km": "120", "description": "Thuis → Utrecht"}
    assert app(event("POST", "/rit", form, cookie))["statusCode"] == 303
    (manual,) = store.legs("car", date(2026, 10, 9), date(2026, 10, 9))
    assert manual.source is LegSource.MANUAL and int(manual.business_km) == 120
    form = {"csrf": token, "date": "2026-10-09", "leg": manual.id}
    assert app(event("POST", "/rit/verwijder", form, cookie))["statusCode"] == 303
    assert store.legs("car", date(2026, 10, 9), date(2026, 10, 9)) == []


def test_save_unknown_stop_as_business_location(env):
    app, store, mails, _ = env
    store.put_observations(
        observations(
            date(2026, 10, 8),
            [
                ("00:00", "07:30", "home", 41_000),
                ("08:30", "16:30", "client", 41_071),
                ("17:40", "21:10", "unknown", 41_109),
                ("22:00", "23:00", "home", 41_160),
            ],
        )
    )
    rederive(store, "car", datetime(2026, 10, 8, 21, 30, tzinfo=UTC))
    cookie = login(app, mails)
    day = date(2026, 10, 8)
    seg = store.legs("car", day, day)[1].segment_id
    form = {
        "csrf": csrf(app, cookie),
        "segment": seg,
        "lat": str(UNKNOWN[0]),
        "lon": str(UNKNOWN[1]),
        "name": "Kantoor Amsterdam",
        "type": "business",
        "next": f"/dag/{day}",
    }
    assert app(event("POST", "/stop", form, cookie))["statusCode"] == 303
    loc = next(loc for loc in store.locations() if loc.name == "Kantoor Amsterdam")
    assert loc.type is LocationType.BUSINESS
    legs = store.legs("car", day, day)
    assert int(sum(leg.business_km for leg in legs)) == 160
    assert all(leg.status is LegStatus.AUTO for leg in legs)  # routes created: rule 1
    assert {r.distance_km for r in store.routes() if loc.id in (r.location_a, r.location_b)} == {
        38,
        51,
    }


def test_year_page_stamdata_page_and_xlsx(env):
    app, store, mails, _ = env
    store.put_leg(manual(date(2026, 9, 14), 88))
    cookie = login(app, mails)
    body = app(event("GET", "/jaar/2026", cookie=cookie))["body"]
    assert "Jaaroverzicht 2026" in body and "september" in body
    body = app(event("GET", "/stamdata", cookie=cookie))["body"]
    assert "Thuis" in body and "A4" in body
    resp = app(event("GET", "/maand/2026-09.xlsx", cookie=cookie))
    assert resp["isBase64Encoded"] and "attachment" in resp["headers"]["Content-Disposition"]
    assert app(event("GET", "/nope", cookie=cookie))["statusCode"] == 404


def test_route_save_keeps_one_default(env):
    app, store, mails, _ = env
    cookie = login(app, mails)
    form = {
        "csrf": csrf(app, cookie),
        "from": "home",
        "to": "client",
        "name": "A12",
        "km": "75",
        "default": "1",
    }
    assert app(event("POST", "/stamdata/route", form, cookie))["statusCode"] == 303
    defaults = [r.name for r in store.routes() if r.is_default]
    assert defaults == ["A12"]


def test_lambda_handler_logs_without_query(env, capsys):
    app, *_ = env
    resp = web_handler.handler(event("GET", "/login", query={"t": "secret"}), None, app=app)
    assert resp["statusCode"] == 200
    out = capsys.readouterr().out
    assert '"path": "/login"' in out and "secret" not in out


def test_request_parses_base64_body():
    import base64

    ev = event("POST", "/x", {"a": "1"})
    ev["body"] = base64.b64encode(ev["body"].encode()).decode()
    ev["isBase64Encoded"] = True
    assert Request.from_event(ev).form == {"a": "1"}


def test_day_page_shows_odometer_and_arrival_map_per_trip(env):
    app, store, mails, _ = env
    day = date(2026, 10, 8)
    obs = observations(
        day,
        [
            ("00:00", "07:30", "home", 41_000),
            ("08:30", "16:30", "client", 41_071),
            ("19:00", "23:00", "home", 41_141),
        ],
    )
    driving = datetime(2026, 10, 8, 17, 58, tzinfo=AMSTERDAM)
    poll = datetime(2026, 10, 8, 18, 0, tzinfo=AMSTERDAM)
    obs.append(Observation("car", poll, 41_138, driving, *UNKNOWN, driving))
    store.put_observations(obs)
    rederive(store, "car", datetime(2026, 10, 8, 21, 30, tzinfo=UTC))
    legs = store.legs("car", day, day)
    assert [int(leg.distance_km) for leg in legs] == [71, 70]
    body = app(event("GET", f"/dag/{day}", cookie=login(app, mails)))["body"]
    assert "70 km gereden (km-stand 41.071 → 41.141)" in body
    assert "onderweg" not in body  # pass-through points only in the readings table
    assert body.count('class="map"') == 2  # arrival at the client and at home
    assert "<td>41.138</td>" in body
