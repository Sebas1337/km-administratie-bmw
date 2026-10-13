from pathlib import Path

import pytest

from kmlog import stamdata
from kmlog.cli import main
from kmlog.model import LocationType
from kmlog.store.sqlite import SqliteStore

EXAMPLE = Path(__file__).parent.parent / "data/samples/stamdata.example.toml"


def test_example_file_parses():
    data = stamdata.load(EXAMPLE)
    assert [(loc.id, loc.type) for loc in data.locations] == [
        ("home", LocationType.HOME),
        ("client", LocationType.CLIENT),
    ]
    assert [(r.id, r.distance_km, r.is_default) for r in data.routes] == [
        ("a4", 70, True),
        ("ams", 84, False),
    ]
    assert data.rates[0].cents_per_km == 25


def test_route_to_unknown_location_is_rejected():
    with pytest.raises(ValueError, match="unknown location"):
        stamdata.parse({"route": [{"id": "x", "from": "home", "to": "mars", "km": 1}]})


def test_two_defaults_for_one_pair_are_rejected():
    locs = [
        {"id": i, "type": "home" if i == "home" else "client", "lat": 0, "lon": 0}
        for i in ("home", "client")
    ]
    routes = [
        {"id": r, "from": "home", "to": "client", "km": 70, "default": True} for r in ("a", "b")
    ]
    with pytest.raises(ValueError, match="more than one default"):
        stamdata.parse({"location": locs, "route": routes})


def test_cli_replaces_places(tmp_path, capsys):
    db = tmp_path / "km.sqlite"
    main(["--db", str(db), "stamdata", str(EXAMPLE)])
    smaller = tmp_path / "s.toml"
    smaller.write_text('[[location]]\nid = "home"\ntype = "home"\nlat = 1\nlon = 2\n')
    with pytest.raises(SystemExit, match="wijkt af"):  # guard: store differs from the file
        main(["--db", str(db), "stamdata", str(smaller)])
    main(["--db", str(db), "stamdata", str(smaller), "--force"])
    store = SqliteStore(db)
    assert [loc.id for loc in store.locations()] == ["home"]
    assert store.routes() == []
    assert store.rate(2026).cents_per_km == 25  # rates are kept
    assert "2 locaties, 2 routes, 1 tarieven" in capsys.readouterr().out


def test_export_roundtrip_and_same_file_is_allowed(tmp_path, capsys):
    db = tmp_path / "km.sqlite"
    main(["--db", str(db), "stamdata", str(EXAMPLE)])
    main(["--db", str(db), "stamdata", str(EXAMPLE)])  # identical: no --force needed
    store = SqliteStore(db)
    store.put_location(
        stamdata.Location("office", 'Kantoor "A\'dam"', LocationType.BUSINESS, 52.3, 4.9, 150)
    )
    out = tmp_path / "export.toml"
    main(["--db", str(db), "stamdata", "--export", str(out)])
    exported = stamdata.load(out)
    assert {loc.id for loc in exported.locations} == {"home", "client", "office"}
    assert next(loc for loc in exported.locations if loc.id == "office").name == 'Kantoor "A\'dam"'
    assert len(exported.routes) == 2 and exported.rates[0].cents_per_km == 25
    main(["--db", str(db), "stamdata", str(out)])  # export == store: allowed
