"""A small static map from OpenStreetMap tiles, centred on one point — no JavaScript needed.

The page places a few 256-px tiles absolutely inside a fixed-size box so that (lat, lon) lands in
the middle, where a CSS marker sits. Tiles come straight from tile.openstreetmap.org (allowed in
the CSP img-src); the <img> tags send a Referer (origin only), which the OSM tile usage policy
requires for browser requests. Attribution is shown below the map.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TILE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_ORIGIN = "https://tile.openstreetmap.org"


@dataclass(frozen=True)
class Tile:
    url: str
    left: int
    top: int


@dataclass(frozen=True)
class StaticMap:
    width: int
    height: int
    tiles: list[Tile]


def world_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Web-Mercator pixel coordinates of (lat, lon) at `zoom` (whole world = 256·2^zoom px)."""
    scale = TILE * 2**zoom
    x = (lon + 180.0) / 360.0 * scale
    lat_rad = math.radians(max(min(lat, 85.0511), -85.0511))
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * scale
    return x, y


def static_map(lat: float, lon: float, zoom: int = 15, width: int = 320, height: int = 220):
    px, py = world_pixel(lat, lon, zoom)
    x0, y0 = px - width / 2, py - height / 2  # world pixel of the box's top-left corner
    n = 2**zoom
    tiles = []
    for ty in range(math.floor(y0 / TILE), math.floor((y0 + height) / TILE) + 1):
        if not 0 <= ty < n:
            continue
        for tx in range(math.floor(x0 / TILE), math.floor((x0 + width) / TILE) + 1):
            tiles.append(
                Tile(
                    url=TILE_URL.format(z=zoom, x=tx % n, y=ty),
                    left=round(tx * TILE - x0),
                    top=round(ty * TILE - y0),
                )
            )
    return StaticMap(width=width, height=height, tiles=tiles)
