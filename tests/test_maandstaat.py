from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import openpyxl
import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

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
    DayRow,
    Maandstaat,
    TripRow,
    build_jaaroverzicht,
    build_maandstaat,
    format_euro,
    write_jaaroverzicht,
    write_maandstaat,
)
from kmlog.report.pdf import write_jaaroverzicht_pdf, write_maandstaat_pdf
from kmlog.report.pdfwriter import clean, text_width, wrap
from kmlog.store.sqlite import SqliteStore
from test_stamdata import EXAMPLE
from test_voorbeelddagen import (
    LOCATIONS,
    WORKDAY,
    _day_c,
    _private_day,
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
        obs,
    )
    assert [(r.date.day, r.description) for r in m.rows] == [
        (5, "handmatige rit"),
        (6, "Motor"),
        (7, "Thuis → Klant → Thuis"),
        (8, "Thuis → Klant → onbekend → Thuis"),
    ]
    day7, day8 = m.rows[2], m.rows[3]
    assert (day7.driven_km, day7.business_km) == (141, 141)
    assert (day7.odometer_from, day7.odometer_to) == (40_000, 40_141)
    assert (m.rows[0].odometer_from, m.rows[0].odometer_to) == (None, None)  # manual trip
    assert (day8.driven_km, day8.business_km, day8.private_km) == (160, 141, 19)
    assert not day8.open_confirmation  # rule 3: stop at an unknown place, no question
    assert m.business_km == 140 + 141 + 141
    assert m.amount_cents == 422 * 25
    assert (m.odometer_start, m.odometer_end) == (40_000, 40_141)
    assert m.open_confirmations == []


def test_answer_changes_maandstaat():
    legs, segments = run(WORKDAY, normal_day(96))
    apply_answer(segments[-1], Answer.DETOUR)
    m = build_maandstaat(2026, 10, CAR, RATE, legs, locations=LOCATIONS)
    assert m.business_km == 167
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

    main(["--db", str(db), "maandstaat", "2026", "9", "--out", str(tmp_path / "sep.pdf")])
    main(["--db", str(db), "jaaroverzicht", "2026", "--pdf", "--out", str(tmp_path / "jaar")])
    assert "Maandstaat september 2026" in _pdf_text(tmp_path / "sep.pdf")
    assert "Jaaroverzicht 2026" in _pdf_text(tmp_path / "jaar")


def _pdf_text(path) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(path).pages)


def _excel_words(path) -> set[str]:
    """Every value in the workbook, as the PDF writes it, split in words (a PDF cell wraps)."""
    words: set[str] = set()
    for ws in load_workbook(path):
        for row in ws.iter_rows(values_only=True):
            for value in row:
                if isinstance(value, datetime):
                    text = f"{value:%d-%m-%Y}"
                elif isinstance(value, int):
                    text = f"{value:,}".replace(",", ".")
                elif isinstance(value, float | Decimal):
                    text = format_euro(round(Decimal(str(value)) * 100))
                else:
                    text = str(value or "")
                words.update(text.split())
    return words


@pytest.mark.parametrize("km", [70, 96])  # all fits; rule 2 with an open question
def test_pdf_shows_every_value_of_the_excel(tmp_path, km):
    legs, _ = run(WORKDAY, normal_day(km))
    m = build_maandstaat(
        2026, 10, CAR, RATE, [manual(date(2026, 10, 5), 140), *legs], locations=LOCATIONS
    )
    excel = _excel_words(write_maandstaat(m, tmp_path / "okt.xlsx"))
    pdf = set(_pdf_text(write_maandstaat_pdf(m, tmp_path / "okt.pdf")).split())
    assert excel - pdf == set()


def test_pdf_layout_of_maandstaat_and_jaaroverzicht(tmp_path):
    sep = build_maandstaat(2026, 9, CAR, RATE, [manual(date(2026, 9, 14), 88, "Leverancier")])
    okt_legs, _ = run(WORKDAY, normal_day(70))
    okt = build_maandstaat(
        2026, 10, CAR, RATE, [manual(date(2026, 10, 5), 140), *okt_legs], locations=LOCATIONS
    )
    reader = PdfReader(write_maandstaat_pdf(okt, tmp_path / "okt.pdf"))
    assert reader.metadata.title == "Maandstaat oktober 2026"
    pages = [page.extract_text() for page in reader.pages]
    assert len(pages) == 2  # declaration per day, then the trips
    day, trips = pages
    assert "Maandstaat oktober 2026" in day and "BMW i4 (AB-123-C)" in day
    assert "€ 0,25 per km" in day and "€ 70,25" in day  # 281 km
    assert "Thuis → Klant → Thuis" in day  # the arrow survives (Symbol font)
    assert "07-10-2026" in day and "40.141" in day  # date and odometer
    assert "pagina 1 van 2" in day
    assert "Ritten oktober 2026" in trips
    assert "Thuis → Klant" in trips and "08:30" in trips and "17:45" in trips
    assert "pagina 2 van 2" in trips

    jaar = _pdf_text(write_jaaroverzicht_pdf(2026, CAR, [okt, sep], tmp_path / "jaar.pdf"))
    assert "Jaaroverzicht 2026" in jaar
    assert jaar.index("Maandstaat september 2026") < jaar.index("Maandstaat oktober 2026")
    assert "€ 92,25" in jaar  # 369 km over both months
    assert "Ritten oktober" not in jaar  # like the Excel: no trips in the jaaroverzicht


def test_pdf_open_confirmation_and_empty_month(tmp_path):
    legs, _ = run(WORKDAY, normal_day(96))  # rule 2: longer than expected, a question
    m = build_maandstaat(2026, 10, CAR, RATE, legs, locations=LOCATIONS)
    text = _pdf_text(write_maandstaat_pdf(m, tmp_path / "okt.pdf"))
    assert "Openstaande bevestigingen" in text and "te bevestigen" in text

    empty = build_maandstaat(2026, 11, CAR, RATE, [])
    text = _pdf_text(write_maandstaat_pdf(empty, tmp_path / "nov.pdf"))
    assert "Geen zakelijke ritten in deze maand." in text and "€ 0,00" in text


def test_private_only_days_and_trips_are_left_out_of_the_pdf(tmp_path):
    private, _ = _private_day(WORKDAY)  # home -> unknown -> home, 0 km business
    mixed, _ = run(
        WORKDAY + timedelta(days=1),
        [
            ("08:00", "09:00", "home", 0),
            ("10:30", "16:00", "client", 71),
            ("17:30", "18:00", "home", 141),
            ("19:00", "20:00", "unknown", 164),  # a private trip after work
            ("21:00", "22:00", "home", 187),
        ],
    )
    m = build_maandstaat(2026, 10, CAR, RATE, [*private, *mixed], locations=LOCATIONS)
    assert [(r.date, r.declared) for r in m.rows] == [
        (WORKDAY, False),
        (WORKDAY + timedelta(days=1), True),
    ]
    assert any(not t.declared for t in m.trips if t.date == WORKDAY + timedelta(days=1))

    reader = PdfReader(write_maandstaat_pdf(m, tmp_path / "okt.pdf"))
    day, trips = [page.extract_text() for page in reader.pages]
    assert "07-10-2026" not in day + trips  # the private-only day is nowhere
    assert f"{WORKDAY + timedelta(days=1):%d-%m-%Y}" in day
    assert "onbekend" in day  # the day row describes the whole day, as in the Excel
    assert "Thuis → onbekend" not in trips and "onbekend → Thuis" not in trips  # no private rows

    excel = load_workbook(write_maandstaat(m, tmp_path / "okt.xlsx"))
    excel_trips = [r[4] for r in excel.worksheets[1].iter_rows(min_row=5, values_only=True) if r[0]]
    assert excel_trips == ["Thuis → Klant", "Klant → Thuis"]  # the same rows as in the PDF
    assert all(trip in trips for trip in excel_trips)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Café Één ë ï € × ± – —", "Café Één ë ï € × ± – —"),  # the PDF can show these
        ("Thuis → Klant", "Thuis → Klant"),
        ("Łódź Plzeň Říčany Şişli București", "Lódz Plzen Rícany Sisli Bucuresti"),
        ("ĳsselstein", "ijsselstein"),
        ("≤ 5 − 2", "<= 5 - 2"),
        ("Klant 🚗", "Klant "),
        ("Αθήνα 東京", "????? ??"),  # another alphabet: visibly incomplete
        ("a\nb\tc", "a b c"),
        (None, ""),
    ],
)
def test_pdf_text_only_has_characters_the_pdf_can_show(text, expected):
    assert clean(text) == expected


def test_pdf_replaces_special_characters_on_the_page(tmp_path):
    day = date(2026, 10, 7)
    m = Maandstaat(CAR, 2026, 10, 25, rows=[DayRow(day, "Thuis → Łódź 🚗 Αθ", 70, 70, 0, "Plzeň")])
    text = _pdf_text(write_maandstaat_pdf(m, tmp_path / "okt.pdf"))
    assert "Thuis → Lódz ??" in text and "Plzen" in text
    assert "■" not in text  # and the Excel keeps the name as it is
    excel = load_workbook(write_maandstaat(m, tmp_path / "okt.xlsx")).active
    assert excel.cell(9, 3).value == "Thuis → Łódź 🚗 Αθ"


@pytest.mark.parametrize("length", [800, 5000, 20000])
def test_pdf_row_taller_than_a_page_continues_on_the_next(tmp_path, length):
    day = date(2026, 10, 7)
    note = "woord " * (length // 6)
    m = Maandstaat(
        CAR,
        2026,
        10,
        25,
        rows=[DayRow(day, "Thuis → Klant", 70, 70, 0, note)],
        trips=[TripRow(day, "Thuis → Klant", "leg", driven_km=70, business_km=70, remark=note)],
    )
    reader = PdfReader(write_maandstaat_pdf(m, tmp_path / "okt.pdf"))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert text.count("woord") == 2 * (length // 6)  # nothing lost, on the day and the trip row
    assert len(reader.pages) > 1
    assert all("Datum" in page.extract_text() for page in reader.pages)  # repeated header


def test_pdf_wraps_a_long_word_in_reasonable_time(tmp_path):
    day = date(2026, 10, 7)
    word = "x" * 200_000
    m = Maandstaat(CAR, 2026, 10, 25, rows=[DayRow(day, word, 70, 70, 0)])
    assert PdfReader(write_maandstaat_pdf(m, tmp_path / "okt.pdf")).pages
    lines = wrap("kort " + "x" * 500 + " einde", 100)
    assert lines[0] == "kort" and lines[-1].endswith("einde")
    assert all(text_width(line) <= 100 for line in lines)


def test_pdf_footer_with_a_long_vehicle_name_stays_left_of_the_middle(tmp_path):
    long_name = Vehicle("car", "N" * 60, "AB-123-C", VehicleKind.CAR, True)
    m = Maandstaat(long_name, 2026, 10, 25)
    reader = PdfReader(write_maandstaat_pdf(m, tmp_path / "okt.pdf"))
    text = reader.pages[0].extract_text()
    assert "(AB-123-C)" in text  # complete at the top of the page
    assert text.count("(AB-123-C)") == 1 and "…" in text  # shortened in the footer


def test_pdf_file_structure_and_empty_year(tmp_path):
    m = build_maandstaat(2026, 11, CAR, RATE, [])
    moment = datetime(2026, 12, 1, 5, 0, tzinfo=UTC)
    path = write_maandstaat_pdf(m, tmp_path / "nov.pdf", generated_at=moment)
    reader = PdfReader(path, strict=True)  # a damaged xref or object fails here
    assert reader.metadata.author == "km-admin"
    assert reader.metadata.creation_date_raw == "D:20261201060000+01'00'"
    assert "gemaakt 01-12-2026 06:00" in reader.pages[0].extract_text()
    text = _pdf_text(write_jaaroverzicht_pdf(2026, CAR, [], tmp_path / "leeg.pdf"))
    assert "Jaaroverzicht 2026" in text and "Totaal" in text


def test_trips_one_row_per_leg():
    legs, _ = _day_c()  # client -> unknown -> home: rule 3
    m = build_maandstaat(2026, 10, CAR, RATE, legs, locations=LOCATIONS)
    assert len(m.rows) == 1 and len(m.trips) == 3
    assert [t.description for t in m.trips] == [
        "Thuis → Klant",
        "Klant → onbekend",
        "onbekend → Thuis",
    ]
    assert [t.explanation for t in m.trips] == [
        "Zakelijk: past bij de verwachte 70 km (t/m 80 km).",
        *["Tussenstop: zakelijk telt de verwachte 70 km, de rest is privé."] * 2,
    ]
    assert sum(t.business_km for t in m.trips) == m.business_km


def test_open_question_marks_the_trip():
    legs, _ = run(WORKDAY, normal_day(96))  # rule 2: longer than expected
    m = build_maandstaat(2026, 10, CAR, RATE, legs, locations=LOCATIONS)
    assert [t.open_confirmation for t in m.trips] == [False, True]
    assert m.trips[1].remark == "te bevestigen"
    assert m.open_confirmations == m.rows


def test_review_lists():
    private, _ = _private_day(WORKDAY)  # home -> unknown 23 -> home 23
    short, _ = run(  # 8 km there and back: below the 10 km of the list
        date(2026, 10, 8),
        [
            ("00:00", "10:00", "home", 41_000),
            ("10:40", "14:00", "unknown", 41_004),
            ("14:40", "23:00", "home", 41_008),
        ],
    )
    weekend, _ = run(date(2026, 10, 10), normal_day(70, out_km=70))  # Saturday at the client
    m = build_maandstaat(2026, 10, CAR, RATE, [*private, *short, *weekend], locations=LOCATIONS)
    (place,) = m.unknown_places
    assert [(v.date, v.km) for v in place.visits] == [(WORKDAY, 46)]
    assert place.visits[0].departed_after is not None
    assert [(t.name, t.dates) for t in m.weekend_trips] == [("Klant", [date(2026, 10, 10)])]


def test_cli_version(capsys):
    from kmlog.version import VERSION

    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.strip() == f"kmlog {VERSION}"


def test_private_only_days_and_trips_are_left_out_of_the_excel(tmp_path):
    private, _ = _private_day(WORKDAY)  # home -> unknown -> home, 0 km business
    errand = [  # 3 km home -> home, then a normal client day
        ("00:00", "06:00", "home", 41_000),
        ("06:40", "07:30", "home", 41_003),
        ("08:30", "16:30", "client", 41_074),
        ("17:45", "23:00", "home", 41_144),
    ]
    mixed, _ = run(date(2026, 10, 8), errand)
    first, _ = run(date(2026, 10, 9), normal_day(31, "client2", out_km=75))  # open question
    notes = [DayNote("car", date(2026, 10, 6), "Motor")]
    m = build_maandstaat(2026, 10, CAR, RATE, [*private, *mixed, *first], notes, LOCATIONS)
    # the month page shows everything
    assert [(r.date.day, r.declared) for r in m.rows] == [
        (6, True),
        (7, False),
        (8, True),
        (9, True),
    ]
    assert (m.rows[2].driven_km, m.rows[2].private_km) == (144, 3)  # the day is complete
    assert len(m.trips) == 7

    wb = load_workbook(write_maandstaat(m, tmp_path / "okt.xlsx"))
    table = wb.worksheets[0].iter_rows(min_row=9, max_row=8 + 4, values_only=True)
    days = [r[2] for r in table if isinstance(r[1], str)]  # rows with a weekday
    assert days == ["Motor", "Thuis → Thuis → Klant → Thuis", "Thuis → Klant 2 → Thuis"]
    trips = [r[4] for r in wb.worksheets[1].iter_rows(min_row=5, values_only=True) if r[0]]
    assert trips == ["Thuis → Klant", "Klant → Thuis", "Thuis → Klant 2", "Klant 2 → Thuis"]


def test_clock_shows_the_day_when_it_differs():
    from datetime import UTC, datetime

    from kmlog.report.maandstaat import clock

    ts = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)  # di 27-10 08:00 Amsterdam
    assert clock(ts, date(2026, 10, 27)) == "08:00"
    assert clock(ts, date(2026, 10, 26)) == "di 27-10 08:00"
    assert clock(None, date(2026, 10, 26)) is None
