"""Search an address or company name (OpenStreetMap Nominatim) to prefill a new location.

Only used for explicit searches by the user, never in the background, and without autocomplete: the
public Nominatim server allows that, with at most 1 request per second, an identifying User-Agent,
attribution and cached results (https://operations.osmfoundation.org/policies/nominatim/). The
server must stay replaceable without a code change, hence the configurable base URL.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import requests

DEFAULT_URL = "https://nominatim.openstreetmap.org"
DEFAULT_CONTACT = "https://github.com/Sebas1337/km-administratie-bmw"
MAX_RESULTS = 5
MAX_QUERY = 120
HOME_BIAS_DEG = 0.5  # ~35-55 km around home: ranks nearby hits first, does not exclude others
CACHE_SECONDS = 24 * 3600
CACHE_SIZE = 100
TIMEOUT = 5


class GeocodeError(Exception):
    """The search service failed or is unreachable; the message is safe to show."""


class RateLimited(GeocodeError):
    pass


@dataclass(frozen=True)
class Place:
    name: str
    address: str
    lat: float
    lon: float


class Geocoder(Protocol):
    def search(self, query: str, near: tuple[float, float] | None = None) -> list[Place]: ...


class NominatimGeocoder:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_URL,
        contact: str = DEFAULT_CONTACT,
        countries: str = "nl",
        session: Any = None,
        allow_request: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"User-Agent": f"km-admin ({contact})", "Accept-Language": "nl"}
        self.countries = countries
        self.http = session or requests.Session()
        self.allow_request = allow_request  # shared limiter: 1 request per second
        self.clock = clock
        self._cache: dict[tuple, tuple[float, list[Place]]] = {}

    def search(self, query: str, near: tuple[float, float] | None = None) -> list[Place]:
        query = " ".join(query.split())[:MAX_QUERY]
        if not query:
            return []
        key = (query.casefold(), self.countries, near and (round(near[0], 1), round(near[1], 1)))
        now = self.clock()
        if (hit := self._cache.get(key)) and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        if not self.allow_request():
            raise RateLimited("Even geduld: één zoekopdracht per seconde. Probeer het zo opnieuw.")
        params: dict[str, Any] = {
            "q": query,
            "format": "jsonv2",
            "limit": MAX_RESULTS * 2,  # room to drop bus stops and the like
            "addressdetails": 1,
            "dedupe": 1,
            "countrycodes": self.countries,
        }
        if near:
            lat, lon = near
            d = HOME_BIAS_DEG
            params["viewbox"] = f"{lon - d},{lat + d},{lon + d},{lat - d}"
        try:
            resp = self.http.get(
                f"{self.base_url}/search", params=params, headers=self.headers, timeout=TIMEOUT
            )
            if not resp.ok:
                raise GeocodeError(f"HTTP {resp.status_code}")
            places = _parse(resp.json())
        except (
            requests.RequestException,
            GeocodeError,
            ValueError,
            LookupError,
            TypeError,
            AttributeError,
        ) as e:
            raise GeocodeError(
                "De zoekdienst (OpenStreetMap) gaf geen antwoord. Vul de locatie handmatig in."
            ) from e
        if len(self._cache) >= CACHE_SIZE:
            self._cache.clear()
        self._cache[key] = (now, places)
        return places


def _parse(rows: list[dict]) -> list[Place]:
    places: list[Place] = []
    for r in rows:
        if r.get("category") == "highway":  # bus stops, bare streets
            continue
        a = r.get("address") or {}
        street = " ".join(x for x in (a.get("road"), a.get("house_number")) if x)
        town = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or ""
        address = ", ".join(x for x in (street, f"{a.get('postcode', '')} {town}".strip()) if x)
        name = r.get("name") or street
        if not name:
            continue
        places.append(Place(name=name, address=address, lat=float(r["lat"]), lon=float(r["lon"])))
        if len(places) == MAX_RESULTS:
            break
    return places
