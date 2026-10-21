"""Address/company search (kmlog.geocode): parsing, bias, caching, limits, failure modes."""

import pytest
import requests

from kmlog.geocode import GeocodeError, NominatimGeocoder, Place, RateLimited
from test_cardata import Resp

COMPANY = {
    "category": "office",
    "name": "Rijkswaterstaat",
    "lat": "52.05852",
    "lon": "5.10193",
    "address": {
        "road": "Griffioenlaan",
        "house_number": "2",
        "postcode": "3526 LA",
        "city": "Utrecht",
    },
}
BUS_STOP = {**COMPANY, "category": "highway", "address": {"road": "Griffioenlaan"}}
HOUSE = {
    "category": "place",
    "lat": "52.37329",
    "lon": "4.89372",
    "address": {"road": "Dam", "house_number": "1", "postcode": "1012 JS", "town": "Amsterdam"},
}
NO_NAME = {"category": "place", "lat": "1", "lon": "2", "address": {"postcode": "1234 AB"}}


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def geocoder(http, **kwargs):
    return NominatimGeocoder(session=http, **kwargs)


def test_company_and_address_results_without_bus_stops_or_nameless_hits():
    http = FakeHttp(Resp(200, [BUS_STOP, COMPANY, NO_NAME, HOUSE]))
    assert geocoder(http).search("Rijkswaterstaat Utrecht") == [
        Place("Rijkswaterstaat", "Griffioenlaan 2, 3526 LA Utrecht", 52.05852, 5.10193),
        Place("Dam 1", "Dam 1, 1012 JS Amsterdam", 52.37329, 4.89372),
    ]


def test_request_identifies_the_app_and_limits_to_the_configured_countries():
    http = FakeHttp(Resp(200, []))
    geocoder(http, contact="https://example.org/km", countries="nl,be").search("  Dam   1  ")
    url, kwargs = http.calls[0]
    assert url == "https://nominatim.openstreetmap.org/search"
    assert kwargs["headers"]["User-Agent"] == "km-admin (https://example.org/km)"
    assert kwargs["params"]["q"] == "Dam 1"
    assert kwargs["params"]["countrycodes"] == "nl,be"
    assert "viewbox" not in kwargs["params"]  # no home known: no bias


def test_home_bias_is_a_viewbox_not_a_filter():
    http = FakeHttp(Resp(200, []))
    geocoder(http).search("Philips", near=(51.44, 5.47))
    params = http.calls[0][1]["params"]
    assert params["viewbox"] == "4.97,51.94,5.97,50.94"
    assert "bounded" not in params


def test_empty_query_makes_no_request():
    http = FakeHttp(Resp(200, []))
    assert geocoder(http).search("   ") == []
    assert http.calls == []


def test_same_query_is_cached_for_a_day():
    now = [1000.0]
    http = FakeHttp(Resp(200, [COMPANY]))
    g = geocoder(http, clock=lambda: now[0])
    g.search("Rijkswaterstaat")
    g.search("rijkswaterstaat")  # case-insensitive
    assert len(http.calls) == 1
    now[0] += 25 * 3600
    g.search("Rijkswaterstaat")
    assert len(http.calls) == 2


def test_rate_limit_stops_before_the_request_and_cached_hits_do_not_count():
    http = FakeHttp(Resp(200, [COMPANY]))
    allowed = iter([True, False])
    g = geocoder(http, allow_request=lambda: next(allowed))
    g.search("a")
    g.search("a")  # cache hit: the limiter is not asked
    with pytest.raises(RateLimited):
        g.search("b")
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "outcome",
    [Resp(429, {}), Resp(503, {}), Resp(200, "not a list"), requests.Timeout("slow")],
)
def test_failures_become_one_friendly_error_and_are_not_cached(outcome):
    http = FakeHttp(outcome)
    g = geocoder(http)
    for _ in range(2):
        with pytest.raises(GeocodeError, match="handmatig"):
            g.search("Dam 1")
    assert len(http.calls) == 2
