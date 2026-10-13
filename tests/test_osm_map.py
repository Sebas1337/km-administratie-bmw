from kmlog.web.osm_map import TILE, static_map, world_pixel


def test_world_pixel_known_points():
    assert world_pixel(0.0, 0.0, 0) == (128.0, 128.0)  # centre of the single zoom-0 tile
    x, y = world_pixel(52.0894, 5.1100, 15)
    assert (int(x // TILE), int(y // TILE)) == (
        16849,
        10810,
    )  # OSM tile containing Utrecht Centraal (slippy-map formula)


def test_static_map_centres_the_point():
    lat, lon, zoom, w, h = 52.3791, 4.9003, 15, 480, 240
    m = static_map(lat, lon, zoom, w, h)
    px, py = world_pixel(lat, lon, zoom)
    # the tile under the box centre must contain the point at the right offset
    centre = [
        t for t in m.tiles if t.left <= w / 2 < t.left + TILE and t.top <= h / 2 < t.top + TILE
    ]
    assert len(centre) == 1
    t = centre[0]
    z, x, y = map(int, t.url.removesuffix(".png").split("/")[-3:])
    assert (z, x, y) == (zoom, int(px // TILE), int(py // TILE))
    assert abs((w / 2 - t.left) - (px % TILE)) <= 1 and abs((h / 2 - t.top) - (py % TILE)) <= 1
    # the tiles cover the whole box
    assert min(t.left for t in m.tiles) <= 0 and max(t.left for t in m.tiles) + TILE >= w
    assert min(t.top for t in m.tiles) <= 0 and max(t.top for t in m.tiles) + TILE >= h
