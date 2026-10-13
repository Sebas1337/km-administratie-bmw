from datetime import date
from decimal import Decimal

import openpyxl
import pytest

from kmlog.classify import Answer, apply_answer
from kmlog.cli import main
from kmlog.model import (
    Classification,
    DayNote,
    Leg,
    LegSource,
    LegStatus,
    Rate,
    Vehicle,
    VehicleKind,
)
from kmlog.report.maandstaat import (
    build_jaaroverzicht,
    build_maandstaat,
    write_jaaroverzicht,
    write_maandstaat,
)
from kmlog.store.sqlite import SqliteStore
from test_stamdata import EXAMPLE
from test_voorbeelddagen import (
    LOCATIONS,
    ROUTES,
    WORKDAY,
    _day_c,
    normal_day,
    observations,
    run,
)

CAR = Vehicle("car", "BMW i4", "AB-123-C", VehicleKind.CAR, True)
RATE = Rate(2026, 25)


def manual(day: date, km: int, note: str | None = None) -> Leg:
    return Leg(
        id=f"car#manual#{day}",
        vehicle="car",
        date=day,
        distance_km=Decimal(km),
        business_km=Decimal(km),
        classification=Classification.BUSINESS,
        status=LegStatus.CONFIRMED,
        note=note,
        source=LegSource.MANUAL,
    )


def test_maandstaat_mixes_manual_cardata_and_notes():
    a_legs, _ = run(WORKDAY, normal_day(70))  # 7 okt: 141 km
    c_legs, _ = _day_c()  # also 7 okt in the fixture; move it to 8 okt
    for leg in c_legs:
        leg.date = date(2026, 10, 8)
    obs = observations(WORKDAY, normal_day(70))
    m = build_maandstaat(
        2026,
        10,
        CAR,
        RATE,
        [manual(date(2026, 10, 5), 140), *a_legs, *c_legs, manual(date(2026, 9, 30), 140)],
        [DayNote("car", date(2026, 10, 6), "Motor")],
        LOCATIONS,
        ROUTES,
        obs,
    )
    assert [(r.date.day, r.description) for r in m.rows] == [
        (5, "handmatige rit"),
        (6, "Motor"),
        (7, "Thuis → Klant → Thuis"),
        (8, "Thuis → Klant → onbekend → Thuis"),
    ]
    day7, day8 = m.rows[2], m.rows[3]
    assert (day7.driven_km, day7.business_km, day7.routes) == (141, 141, "A4")
    assert (day7.odometer_from, day7.odometer_to) == (40_000, 40_141)
    assert (m.rows[0].odometer_from, m.rows[0].odometer_to) == (None, None)  # manual trip
    assert (day8.driven_km, day8.business_km, day8.private_km) == (160, 141, 19)
    assert day8.open_confirmation and day8.remark.startswith("te bevestigen")
    assert m.business_km == 140 + 141 + 141
    assert m.amount_cents == 422 * 25
    assert (m.odometer_start, m.odometer_end) == (40_000, 40_141)
    assert m.open_confirmations == [day8]


def test_answer_changes_maandstaat():
    legs, segments = _day_c()
    apply_answer(segments[-1], Answer.BUSINESS)
    m = build_maandstaat(2026, 10, CAR, RATE, legs, locations=LOCATIONS)
    assert m.business_km == 160
    assert m.open_confirmations == []


def test_motor_is_never_reimbursed():
    motor = Vehicle("bike", "Motor", "XX-00-X", VehicleKind.MOTOR, reimbursable=False)
    m = build_maandstaat(2026, 10, motor, RATE, [manual(date(2026, 10, 5), 140)])
    assert m.business_km == 140
    assert m.amount_cents == 0


def test_rate_year_must_match():
    with pytest.raises(ValueError):
        build_maandstaat(2027, 1, CAR, RATE, [])


def test_jaaroverzicht_and_xlsx(tmp_path):
    sep = build_maandstaat(2026, 9, CAR, RATE, [manual(date(2026, 9, 14), 88, "Leverancier")])
    okt_legs, _ = run(WORKDAY, normal_day(70))
    okt = build_maandstaat(
        2026, 10, CAR, RATE, [manual(date(2026, 10, 5), 140), *okt_legs], locations=LOCATIONS
    )
    totals = build_jaaroverzicht([okt, sep])
    assert [(t.month, t.trip_days, t.business_km, t.amount_cents) for t in totals] == [
        (9, 1, 88, 2200),
        (10, 2, 281, 7025),
    ]

    path = write_maandstaat(okt, tmp_path / "okt.xlsx")
    ws = openpyxl.load_workbook(path).active
    values = [v for row in ws.iter_rows(values_only=True) for v in row if v is not None]
    assert "Maandstaat oktober 2026" in values
    assert "Thuis → Klant → Thuis" in values
    assert 281 in values

    trips = openpyxl.load_workbook(path)["Ritten oktober 2026"]
    rows = [r for r in trips.iter_rows(min_row=5, values_only=True) if r[0] is not None]
    assert [(r[2], r[3], r[4], r[8], r[9]) for r in rows] == [
        (None, None, "handmatige rit", 140, 140),
        ("07:00", "08:30", "Thuis → Klant", 71, 71),
        ("16:00", "17:45", "Klant → Thuis", 70, 70),
    ]

    path = write_jaaroverzicht(2026, CAR, [sep, okt], tmp_path / "jaar.xlsx")
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["Jaaroverzicht 2026", "September 2026", "Oktober 2026"]


def test_cli_stamdata_and_reports(tmp_path, capsys):
    db = tmp_path / "km.sqlite"
    main(["--db", str(db), "stamdata", str(EXAMPLE)])  # vehicle + rate 2026
    store = SqliteStore(db)
    store.put_leg(manual(date(2026, 9, 14), 88))
    store.put_leg(manual(date(2026, 9, 16), 140))
    store.close()
    main(["--db", str(db), "maandstaat", "2026", "9", "--out", str(tmp_path / "sep.xlsx")])
    main(["--db", str(db), "jaaroverzicht", "2026", "--out", str(tmp_path / "jaar.xlsx")])
    out = capsys.readouterr().out
    assert "Maandstaat september 2026: 228 km = € 57,00" in out
    assert "Totaal 2026: 228 km = € 57,00" in out
    assert (tmp_path / "sep.xlsx").exists() and (tmp_path / "jaar.xlsx").exists()


def test_trips_one_row_per_leg():
    legs, _ = _day_c()  # client -> unknown -> home: rule 4, open question
    m = build_maandstaat(2026, 10, CAR, RATE, legs, locations=LOCATIONS, routes=ROUTES)
    assert len(m.rows) == 1 and len(m.trips) == 3
    assert [t.description for t in m.trips] == [
        "Thuis → Klant",
        "Klant → onbekend",
        "onbekend → Thuis",
    ]
    assert all(t.open_confirmation and t.remark == "te bevestigen" for t in m.trips[1:])
    assert sum(t.business_km for t in m.trips) == m.business_km
