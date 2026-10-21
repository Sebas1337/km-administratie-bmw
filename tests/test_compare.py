from datetime import UTC, datetime
from decimal import Decimal

from kmlog.cli import main
from kmlog.compare import compare
from kmlog.process import rederive
from kmlog.store.sqlite import SqliteStore
from test_aws_handlers import seeded
from test_voorbeelddagen import WORKDAY, normal_day, observations

EVENING = datetime(2026, 10, 7, 21, 30, tzinfo=UTC)


def test_compare_lists_changed_legs_and_leaves_the_source_alone():
    store = seeded(SqliteStore())
    store.put_observations(observations(WORKDAY, normal_day(70)))
    legs = rederive(store, "car", EVENING)
    assert compare(store, "car", WORKDAY, WORKDAY, EVENING) == []

    old = legs[1]
    old.business_km = Decimal(60)  # as an older version might have stored it
    store.put_leg(old)
    (change,) = compare(store, "car", WORKDAY, WORKDAY, EVENING)
    assert (change.leg_id, change.business_delta) == (old.id, 10)
    assert change.description == "client → home"
    assert store.legs("car", WORKDAY, WORKDAY)[1].business_km == 60  # source untouched


def test_cli_vergelijk(tmp_path, capsys):
    db = tmp_path / "km.sqlite"
    store = seeded(SqliteStore(db))
    store.put_observations(observations(WORKDAY, normal_day(70)))
    store.close()
    main(["--db", str(db), "vergelijk", "2026-10-07", "2026-10-07"])
    out = capsys.readouterr().out  # nothing derived yet: both legs are new
    assert "2 rit(ten) anders; zakelijk +141 km (niets opgeslagen)" in out
    assert SqliteStore(db).legs("car", WORKDAY, WORKDAY) == []
