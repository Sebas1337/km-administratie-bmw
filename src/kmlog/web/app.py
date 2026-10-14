"""Web UI: server-rendered HTML for a Lambda Function URL (payload format 2.0).

No JavaScript and no external assets; one user (see auth.py). All texts are Dutch.

Routes
    GET  /                        -> /maand/<huidige maand>
    GET  /maand/YYYY-MM           maandoverzicht + openstaande vragen
    GET  /maand/YYYY-MM.xlsx      maandstaat als Excel
    GET  /dag/YYYY-MM-DD          ritten van één dag: corrigeren, handmatige rit, vragen
    GET  /jaar/YYYY               jaaroverzicht
    GET  /instellingen            auto, tarieven, uitlees-venster, meldingen, locaties, routes
                                  (was /stamdata)
    GET  /auto                    BMW CarData koppelen / ontkoppelen (kmlog.carlink)
    POST /antwoord, /antwoord/herstel, /stop, /correctie, /correctie/herstel, /rit,
         /rit/verwijder, /instellingen/voertuig, /instellingen/tarief,
         /instellingen/tarief/verwijder, /instellingen/uitlezen, /instellingen/meldingen,
         /instellingen/locatie, /instellingen/locatie/verwijder, /instellingen/route,
         /instellingen/route/verwijder,
         /auto/koppelen, /auto/bevestigen, /auto/voertuig, /auto/annuleren, /auto/ontkoppelen,
         /uitloggen
    GET  /login, POST /login (mail link), GET+POST /login/bevestig
"""

from __future__ import annotations

import base64
import calendar
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from jinja2 import Environment, PackageLoader, select_autoescape

from .. import actions, carlink
from ..actions import ActionError, open_questions
from ..cardata import CarDataClient, CarDataError, QuotaExceeded
from ..classify import Answer, Question
from ..derive import AMSTERDAM, amsterdam_date, match_location
from ..model import LegSource, LocationType
from ..process import FLAG_MANUAL, reprocess
from ..report.maandstaat import (
    MONTHS,
    WEEKDAYS,
    build_jaaroverzicht,
    describe,
    format_euro,
    write_maandstaat,
)
from ..report.service import MissingConfig, maandstaat_from_store, rate_for
from ..store import Store
from .auth import Auth, Session
from .osm_map import TILE_ORIGIN, static_map

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RECENT_DAYS = 45  # open questions shown on every page
STAMDATA_REPROCESS_DAYS = 31

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        f"default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data: {TILE_ORIGIN}; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    # not no-referrer: with that policy browsers send "Origin: null" on form posts, which the
    # origin check rejects; same-origin still sends nothing to other sites
    "Referrer-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000",
    "Cache-Control": "no-store",
}

ANSWER_LABELS = {
    Answer.DETOUR: "Omweg (alle km zakelijk)",
    Answer.PRIVATE_STOP: "Privé-stop (standaardroute)",
    Answer.BUSINESS: "Zakelijk",
    Answer.PRIVATE: "Privé",
    Answer.CONFIRM: "Bevestig (zakelijk)",
}
QUESTION_TEXT = {
    Question.DETOUR_OR_PRIVATE_STOP: (
        "Langer dan elke bekende route: omweg door file of privé-stop?"
    ),
    Question.UNKNOWN_STOP: "Tussenstop op een onbekende plek: zakelijk of privé?",
    Question.BUSINESS_OR_PRIVATE: "Rit zonder bekende zakelijke bestemming: zakelijk of privé?",
    Question.WEEKEND: "Zakelijke rit in het weekend of op een feestdag: klopt dat?",
}
LOCATION_TYPES = {
    LocationType.HOME.value: "thuis",
    LocationType.CLIENT.value: "klant",
    LocationType.BUSINESS.value: "zakelijk",
    LocationType.PRIVATE.value: "privé",
}
STATUS_TEXT = {
    "auto": "",
    "needs_confirmation": "te bevestigen",
    "confirmed": "bevestigd",
    "timed_out": "verlopen (standaard)",
}
OK_MESSAGES = {
    "antwoord": "Antwoord opgeslagen.",
    "correctie": "Correctie opgeslagen.",
    "rit": "Rit toegevoegd.",
    "verwijderd": "Verwijderd.",
    "stamdata": "Opgeslagen; de ritten van de afgelopen maand zijn opnieuw berekend.",
    "stop": "Locatie opgeslagen en de rit opnieuw berekend.",
    "opgeslagen": "Opgeslagen.",
    "code": "Bevestig de koppeling op de BMW-pagina en kom dan hier terug.",
    "wacht": "BMW heeft de koppeling nog niet bevestigd. Rond het af op de BMW-pagina en "
    "probeer het opnieuw.",
    "kies": "Gekoppeld. Er staan meerdere auto's op dit BMW-account: kies er één.",
    "gekoppeld": "Auto gekoppeld. De volgende poll (elk heel uur) haalt de eerste km-stand op.",
    "geannuleerd": "Koppelen geannuleerd.",
    "ontkoppeld": "Auto ontkoppeld; de BMW-tokens zijn gewist. Je ritten blijven bewaard.",
}


# --- request / response -------------------------------------------------------------------


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return self.headers.get("host", "")

    @classmethod
    def from_event(cls, event: dict, public_host: str | None = None) -> Request:
        http = event.get("requestContext", {}).get("http", {})
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        # Behind CloudFront the Host header is the Function URL's own; the viewer's host arrives as
        # X-Forwarded-Host. Only the one configured domain is trusted, otherwise anyone calling the
        # Function URL directly could point the login link in the mail at their own site.
        if public_host and headers.get("x-forwarded-host") == public_host:
            headers["host"] = public_host
        cookies = {}
        for c in event.get("cookies") or []:
            name, _, value = c.partition("=")
            cookies[name.strip()] = value.strip()
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8", "replace")
        form = {}
        if "application/x-www-form-urlencoded" in headers.get("content-type", ""):
            form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        return cls(
            method=http.get("method", "GET").upper(),
            path=event.get("rawPath") or "/",
            query={k: v for k, v in (event.get("queryStringParameters") or {}).items()},
            form=form,
            cookies=cookies,
            headers=headers,
        )


@dataclass
class Response:
    status: int = 200
    body: str | bytes = ""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[str] = field(default_factory=list)

    def to_lambda(self) -> dict:
        headers = {**SECURITY_HEADERS, **self.headers}
        headers.setdefault("Content-Type", "text/html; charset=utf-8")
        if isinstance(self.body, bytes):
            return {
                "statusCode": self.status,
                "headers": headers,
                "cookies": self.cookies,
                "body": base64.b64encode(self.body).decode(),
                "isBase64Encoded": True,
            }
        return {
            "statusCode": self.status,
            "headers": headers,
            "cookies": self.cookies,
            "body": self.body,
            "isBase64Encoded": False,
        }


def redirect(location: str, cookies: list[str] | None = None) -> Response:
    return Response(303, "", {"Location": location}, cookies or [])


# a local path (+ query) of plain URL characters; no "//host" or "/\host", and no tab or newline,
# which browsers strip from a Location ("/\t/evil.example" becomes "//evil.example")
_LOCAL_PATH = re.compile(r"/(?![/\\])[A-Za-z0-9\-._~/?=&%]*")


def safe_next(value: str | None, default: str = "/") -> str:
    """Only local paths on this host: an allowlist, so no open redirect after login."""
    if value and _LOCAL_PATH.fullmatch(value):
        return value
    return default


# --- formatting ---------------------------------------------------------------------------


def _local_time(ts: datetime | None) -> str:
    return ts.astimezone(AMSTERDAM).strftime("%H:%M") if ts else "?"


def _day_label(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d-%m}"


def _osm(latlon) -> str | None:
    if not latlon:
        return None
    lat, lon = latlon
    return (
        f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=17/{lat:.6f}/{lon:.6f}"
    )


def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("kmlog.web", "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        euro=format_euro,
        day=_day_label,
        time=_local_time,
        daytime=lambda ts: f"{_day_label(amsterdam_date(ts))} {_local_time(ts)}" if ts else "?",
        osm=_osm,
        km=lambda v: f"{int(v):,}".replace(",", ".") if v is not None else "",
    )
    env.globals.update(
        MONTHS=MONTHS,
        ANSWER_LABELS=ANSWER_LABELS,
        QUESTION_TEXT=QUESTION_TEXT,
        LOCATION_TYPES=LOCATION_TYPES,
        STATUS_TEXT=STATUS_TEXT,
    )
    return env


_ENV = _environment()


# --- the app ------------------------------------------------------------------------------

Mailer = Callable[[str, str], None]  # (subject, text body) -> sends to the one user


class WebApp:
    def __init__(
        self,
        store: Store,
        auth: Auth,
        vehicle: str,
        mailer: Mailer,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        cardata: Callable[[], CarDataClient] | None = None,
        quota_limit: int = 40,
        public_host: str | None = None,
    ) -> None:
        self.store = store
        self.auth = auth
        self.vehicle = vehicle
        self.mailer = mailer
        self.now = now
        self.cardata = cardata  # None: no Secrets Manager (local use), the Auto page says so
        self.quota_limit = quota_limit  # own daily CarData limit, as QUOTA_LIMIT of the poller
        self.public_host = public_host  # own domain behind CloudFront (WEB_DOMAIN), else None

    def __call__(self, event: dict) -> dict:
        return self.handle(Request.from_event(event, self.public_host)).to_lambda()

    # --- dispatch -------------------------------------------------------------------------

    def handle(self, req: Request) -> Response:
        if req.path in ("/login", "/login/bevestig"):
            return self._login(req)
        session = self.auth.session(req.cookies, self.now())
        if session is None:
            if req.method == "GET":
                return redirect("/login?" + urlencode({"next": req.path}))
            return self.page("error.html", None, status=401, message="Niet ingelogd.")
        if req.method == "POST":
            origin = req.headers.get("origin")
            bad_origin = bool(origin) and origin != f"https://{req.host}"
            if bad_origin or not self.auth.check_csrf(session, req.form.get("csrf")):
                return self.page("error.html", session, status=403, message="Ongeldig formulier.")
            try:
                return self._post(req, session)
            except (ActionError, carlink.LinkError) as e:
                return self.page("error.html", session, status=400, message=str(e))
            except QuotaExceeded:
                message = "Het dagquotum voor BMW CarData is op; probeer het morgen opnieuw."
                return self.page("error.html", session, status=429, message=message)
            except CarDataError:
                message = "BMW CarData gaf een fout; probeer het later opnieuw."
                return self.page("error.html", session, status=502, message=message)
        return self._get(req, session)

    def _get(self, req: Request, session: Session) -> Response:
        path = req.path.rstrip("/") or "/"
        today = amsterdam_date(self.now())
        if path == "/":
            return redirect(f"/maand/{today:%Y-%m}")
        if m := re.fullmatch(r"/maand/(\d{4})-(\d{2})(\.xlsx)?", path):
            year, month = int(m.group(1)), int(m.group(2))
            if not 1 <= month <= 12:
                return self.not_found(session)
            if m.group(3):
                return self.xlsx(session, year, month)
            return self.month(req, session, year, month)
        if m := re.fullmatch(r"/dag/(\d{4}-\d{2}-\d{2})", path):
            try:
                day = date.fromisoformat(m.group(1))
            except ValueError:
                return self.not_found(session)
            return self.day(req, session, day)
        if m := re.fullmatch(r"/jaar/(\d{4})", path):
            return self.year(req, session, int(m.group(1)))
        if path == "/instellingen":
            return self.settings_page(req, session)
        if path == "/stamdata":  # old name, kept for bookmarks
            return redirect("/instellingen")
        if path == "/auto":
            return self.car(req, session)
        return self.not_found(session)

    def _post(self, req: Request, session: Session) -> Response:
        f, now, store, v = req.form, self.now(), self.store, self.vehicle
        back = safe_next(f.get("next"))
        match req.path:
            case "/uitloggen":
                return redirect("/login", [self.auth.clear_cookie()])
            case "/antwoord":
                actions.answer(store, v, f.get("segment", ""), f.get("answer", ""), now)
                return redirect(_with_ok(back, "antwoord"))
            case "/antwoord/herstel":
                actions.undo_answer(store, v, f.get("segment", ""), now)
                return redirect(_with_ok(back, "antwoord"))
            case "/stop":
                stop = (_float(f.get("lat")), _float(f.get("lon")))
                actions.save_stop(
                    store, v, f.get("segment", ""), stop, f.get("name", ""), f.get("type", ""), now
                )
                return redirect(_with_ok(back, "stop"))
            case "/correctie":
                actions.correct(
                    store,
                    v,
                    f.get("leg", ""),
                    _date(f.get("date")),
                    actions.parse_km(f.get("business_km", "")),
                    f.get("note", "").strip() or None,
                    now,
                )
                return redirect(_with_ok(back, "correctie"))
            case "/correctie/herstel":
                actions.undo_correction(store, v, f.get("leg", ""), _date(f.get("date")), now)
                return redirect(_with_ok(back, "correctie"))
            case "/rit":
                day = _date(f.get("date"))
                actions.add_manual_trip(
                    store,
                    v,
                    day,
                    actions.parse_km(f.get("km", ""), allow_zero=False),
                    f.get("description", ""),
                    f.get("note", ""),
                )
                return redirect(_with_ok(f"/dag/{day}", "rit"))
            case "/rit/verwijder":
                day = _date(f.get("date"))
                actions.delete_manual_trip(store, v, f.get("leg", ""), day)
                return redirect(_with_ok(f"/dag/{day}", "verwijderd"))
            case "/instellingen/voertuig":
                actions.save_vehicle(store, v, name=f.get("name", ""), plate=f.get("plate", ""))
                return redirect(_with_ok("/instellingen", "opgeslagen"))
            case "/instellingen/tarief":
                actions.save_rate(store, year=_int(f.get("year"), 0), euros=f.get("euros", ""))
                return redirect(_with_ok("/instellingen", "opgeslagen"))
            case "/instellingen/tarief/verwijder":
                actions.delete_rate(store, _int(f.get("year"), 0))
                return redirect(_with_ok("/instellingen", "verwijderd"))
            case "/instellingen/uitlezen":
                actions.save_poll_window(
                    store,
                    start_hour=_int(f.get("start_hour"), -1),
                    end_hour=_int(f.get("end_hour"), -1),
                    quota_limit=self.quota_limit,
                )
                return redirect(_with_ok("/instellingen", "opgeslagen"))
            case "/instellingen/meldingen":
                actions.save_mail_settings(store, missed_days=f.get("missed_days") == "1")
                return redirect(_with_ok("/instellingen", "opgeslagen"))
            case path if path.startswith("/auto/"):
                return self._car_post(path, f)
            case "/instellingen/locatie":
                actions.save_location(
                    store,
                    name=f.get("name", ""),
                    type_=f.get("type", ""),
                    lat=_float(f.get("lat")),
                    lon=_float(f.get("lon")),
                    radius_m=_int(f.get("radius_m"), 200),
                    id_=f.get("id") or None,
                )
                return self._places_changed(now)
            case "/instellingen/locatie/verwijder":
                actions.delete_location(store, f.get("id", ""))
                return self._places_changed(now)
            case "/instellingen/route":
                actions.save_route(
                    store,
                    location_a=f.get("from", ""),
                    location_b=f.get("to", ""),
                    name=f.get("name", ""),
                    km=actions.parse_km(f.get("km", ""), allow_zero=False),
                    is_default=f.get("default") == "1",
                    id_=f.get("id") or None,
                )
                return self._places_changed(now)
            case "/instellingen/route/verwijder":
                actions.delete_route(store, f.get("id", ""))
                return self._places_changed(now)
        return self.not_found(session)

    def _car_post(self, path: str, f: dict[str, str]) -> Response:
        if self.cardata is None:
            raise ActionError("Koppelen kan alleen in de AWS-omgeving (Secrets Manager).")
        client = self.cardata()
        match path:
            case "/auto/koppelen":
                carlink.start(client, f.get("client_id", ""))
                key = "code"
            case "/auto/bevestigen":
                key = {"linked": "gekoppeld", "choose": "kies", "pending": "wacht"}[
                    carlink.confirm(client)
                ]
            case "/auto/voertuig":
                carlink.choose_vehicle(client, f.get("vin", ""))
                key = "gekoppeld"
            case "/auto/annuleren":
                carlink.cancel(client)
                key = "geannuleerd"
            case "/auto/ontkoppelen":
                carlink.unlink(client)
                key = "ontkoppeld"
            case _:
                raise ActionError("Onbekende actie.")
        return redirect(_with_ok("/auto", key))

    def _places_changed(self, now: datetime) -> Response:
        today = amsterdam_date(now)
        reprocess(
            self.store, self.vehicle, today - timedelta(days=STAMDATA_REPROCESS_DAYS), today, now
        )
        return redirect(_with_ok("/instellingen", "stamdata"))

    # --- login ----------------------------------------------------------------------------

    def _login(self, req: Request) -> Response:
        now = self.now()
        if req.path == "/login" and req.method == "GET":
            return self.page("login.html", None, next=safe_next(req.query.get("next")))
        if req.path == "/login" and req.method == "POST":
            if not self.auth.may_send_login_mail(now):
                return self.page(
                    "error.html", None, status=429, message="Te veel inloglinks; probeer later."
                )
            token = self.auth.new_login_token(now)
            link = f"https://{req.host}/login/bevestig?" + urlencode(
                {"t": token, "next": safe_next(req.form.get("next"))}
            )
            self.mailer(
                "Inloggen kilometeradministratie",
                "Klik binnen 15 minuten op deze link om in te loggen:\n\n"
                f"{link}\n\nDe link werkt één keer. Niet aangevraagd? Dan kun je deze mail "
                "negeren.\n",
            )
            return self.page("login_sent.html", None)
        if req.method == "GET":  # /login/bevestig: show a button, don't consume on GET
            return self.page(
                "login_verify.html",
                None,
                token=req.query.get("t", ""),
                next=safe_next(req.query.get("next")),
            )
        if not self.auth.consume_login_token(req.form.get("t", ""), now):
            return self.page(
                "error.html",
                None,
                status=400,
                message="Deze inloglink is verlopen of al gebruikt. Vraag een nieuwe aan.",
                login=True,
            )
        return redirect(safe_next(req.form.get("next")), [self.auth.new_session_cookie(now)])

    # --- pages ----------------------------------------------------------------------------

    def page(self, template: str, session: Session | None, status: int = 200, **ctx) -> Response:
        if session is not None and "open_count" not in ctx:
            ctx["open_count"] = len(self._recent_questions())
        html = _ENV.get_template(template).render(
            session=session,
            csrf=self.auth.csrf_token(session) if session else "",
            today=amsterdam_date(self.now()),
            **ctx,
        )
        return Response(status, html)

    def not_found(self, session: Session | None) -> Response:
        return self.page("error.html", session, status=404, message="Pagina niet gevonden.")

    def _recent_questions(self):
        today = amsterdam_date(self.now())
        legs = self.store.legs(self.vehicle, today - timedelta(days=RECENT_DAYS), today)
        return open_questions(legs)

    def _names(self) -> dict[str, str]:
        return {loc.id: loc.name for loc in self.store.locations()}

    def month(self, req: Request, session: Session, year: int, month: int) -> Response:
        try:
            m = maandstaat_from_store(self.store, self.vehicle, year, month)
        except MissingConfig:
            m = None
        questions = self._recent_questions()
        names = self._names()
        car = self._car_now() if (year, month) == self._this_month() else None
        prev_m = date(year, month, 1) - timedelta(days=1)
        next_m = date(year, month, calendar.monthrange(year, month)[1]) + timedelta(days=1)
        return self.page(
            "month.html",
            session,
            m=m,
            year=year,
            month=month,
            prev=f"{prev_m:%Y-%m}",
            next=f"{next_m:%Y-%m}",
            questions=questions,
            open_count=len(questions),
            car=car,
            describe=lambda legs: describe(legs, names),
            per_trip=req.query.get("weergave") == "ritten",
            ok=OK_MESSAGES.get(req.query.get("ok", "")),
            here=req.path,
        )

    def _this_month(self) -> tuple[int, int]:
        today = amsterdam_date(self.now())
        return today.year, today.month

    def _car_now(self) -> dict | None:
        """Latest reading: the km-stand the ritten are computed from, and where the car was."""
        recent = self.store.observations(self.vehicle, start=self.now() - timedelta(days=14))
        if not recent:
            return None
        o = recent[-1]
        loc = match_location(o.lat, o.lon, self.store.locations())
        return {
            "odometer_km": o.odometer_km,
            "reported_at": o.odometer_ts,
            "polled_at": o.polled_at,
            "place": loc.name if loc else ("onbekend" if o.lat is not None else None),
            "latlon": (o.lat, o.lon) if o.lat is not None else None,
            "map": static_map(o.lat, o.lon, width=480, height=240) if o.lat is not None else None,
        }

    def day(self, req: Request, session: Session, day: date) -> Response:
        legs = self.store.legs(self.vehicle, day, day)
        names = self._names()
        notes = self.store.day_notes(self.vehicle, day, day)
        overrides = self.store.overrides(self.vehicle)
        segment_ids = {leg.segment_id for leg in legs if leg.segment_id}
        questions = [q for q in self._recent_questions() if q.segment_id in segment_ids]
        answers = self.store.answers(self.vehicle)
        start = datetime.combine(day, time(), AMSTERDAM)
        locations = self.store.locations()
        readings = [
            {
                "polled_at": o.polled_at,
                "odometer_km": o.odometer_km,
                "reported_at": o.odometer_ts,
                "place": loc.name if (loc := match_location(o.lat, o.lon, locations)) else None,
                "latlon": (o.lat, o.lon) if o.lat is not None else None,
            }
            for o in self.store.observations(self.vehicle, start, start + timedelta(days=1))
        ]
        answered = {
            sid: answers[sid]
            for sid in segment_ids
            if sid in answers and not any(q.segment_id == sid for q in questions)
        }
        return self.page(
            "day.html",
            session,
            day=day,
            prev=day - timedelta(days=1),
            next=day + timedelta(days=1),
            legs=legs,
            names=names,
            notes=notes,
            readings=readings,
            arrival_maps={
                leg.id: static_map(*leg.to_latlon, width=480, height=200)
                for leg in legs
                if leg.to_latlon
            },
            questions=questions,
            answered=answered,
            overrides=overrides,
            FLAG_MANUAL=FLAG_MANUAL,
            CARDATA=LegSource.CARDATA,
            MANUAL=LegSource.MANUAL,
            describe=lambda legs: describe(legs, names),
            ok=OK_MESSAGES.get(req.query.get("ok", "")),
            here=req.path,
        )

    def year(self, req: Request, session: Session, year: int) -> Response:
        months = []
        for mo in range(1, 13):
            try:
                m = maandstaat_from_store(self.store, self.vehicle, year, mo)
            except MissingConfig:
                continue
            if m.rows:
                months.append(m)
        totals = build_jaaroverzicht(months)
        return self.page(
            "year.html",
            session,
            year=year,
            totals=totals,
            total_km=sum(t.business_km for t in totals),
            total_cents=sum(t.amount_cents for t in totals),
        )

    def car(self, req: Request, session: Session) -> Response:
        link = carlink.status(self.cardata()) if self.cardata else None
        return self.page(
            "car.html",
            session,
            link=link,
            car_now=self._car_now(),
            quota_used=self.store.quota_used(self.now().date()),
            ok=OK_MESSAGES.get(req.query.get("ok", "")),
        )

    def settings_page(self, req: Request, session: Session) -> Response:
        locations = self.store.locations()
        this_year = amsterdam_date(self.now()).year
        rates = self.store.rates()
        rate_years = {r.year for r in rates}
        return self.page(
            "instellingen.html",
            session,
            vehicle=self.store.vehicle(self.vehicle),
            rates=rates,
            rate_years=rate_years,
            next_rate_year=this_year if this_year not in rate_years else max(rate_years) + 1,
            this_year=this_year,
            current_rate=rate_for(self.store, this_year),
            settings=self.store.settings(),
            quota_limit=self.quota_limit,
            locations=locations,
            routes=self.store.routes(),
            names={loc.id: loc.name for loc in locations},
            ok=OK_MESSAGES.get(req.query.get("ok", "")),
        )

    def xlsx(self, session: Session, year: int, month: int) -> Response:
        try:
            m = maandstaat_from_store(self.store, self.vehicle, year, month)
        except MissingConfig:
            return self.page(
                "error.html", session, status=404, message="Geen tarief voor dit jaar."
            )
        with tempfile.TemporaryDirectory() as tmp:
            data = write_maandstaat(m, Path(tmp) / f"{year}-{month:02d}.xlsx").read_bytes()
        return Response(
            200,
            data,
            {
                "Content-Type": XLSX,
                "Content-Disposition": f'attachment; filename="maandstaat-{year}-{month:02d}.xlsx"',
            },
        )


# --- form helpers -------------------------------------------------------------------------


def _with_ok(path: str, key: str) -> str:
    return f"{path}{'&' if '?' in path else '?'}ok={key}"


def _date(text: str | None) -> date:
    try:
        return date.fromisoformat(text or "")
    except ValueError as e:
        raise ActionError("Ongeldige datum.") from e


def _float(text: str | None) -> float:
    try:
        return float((text or "").replace(",", "."))
    except ValueError as e:
        raise ActionError("Ongeldig getal.") from e


def _int(text: str | None, default: int) -> int:
    if not text:
        return default
    try:
        return int(text)
    except ValueError as e:
        raise ActionError("Ongeldig getal.") from e
