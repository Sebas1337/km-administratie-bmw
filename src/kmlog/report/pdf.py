"""Maandstaat and jaaroverzicht as PDF, next to the Excel (ONTWERP §9).

Same content as the Excel, for bookkeeping software (Exact Online and the like) that takes a
PDF as the attachment of a booking and for the archive: the declaration per day, the totals and
the open confirmations, then every declared trip on its own row. Built from the same
`Maandstaat` as the Excel, so both always agree; only characters the PDF cannot show are replaced
(`pdfwriter.clean`). Every page has a footer with the vehicle, version, creation date and
"pagina x van y".
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from ..derive import AMSTERDAM
from ..model import Vehicle
from ..version import VERSION
from .maandstaat import (
    MONTHS,
    WEEKDAYS,
    Maandstaat,
    build_jaaroverzicht,
    clock,
    format_euro,
)
from .pdfwriter import GRAY, MM, WIDTH, Pdf

CREATOR = f"km-admin {VERSION}"
TITLE_SIZE = 15
SMALL = 7.5


def _km(km: int | None) -> str:
    return "" if km is None else f"{km:,}".replace(",", ".")


def _date(d: date) -> str:
    return f"{d:%d-%m-%Y}"


def _vehicle(vehicle: Vehicle) -> str:
    return f"{vehicle.name} ({vehicle.plate})"


def _columns(*widths_mm: float) -> list[float]:
    """Column widths in points; the last column takes the rest of the page."""
    widths = [w * MM for w in widths_mm]
    return [*widths, WIDTH - sum(widths)]


def _maandstaat(pdf: Pdf, m: Maandstaat, with_trips: bool = True) -> None:
    """The maandstaat (declaration per day) and, with `with_trips`, its trips on a new page."""
    pdf.paragraph(m.title, bold=True, size=TITLE_SIZE, after=4)
    pairs = [
        ("Voertuig", _vehicle(m.vehicle)),
        ("Tarief", f"{format_euro(m.cents_per_km)} per km"),
        ("Kilometerstand begin", _km(m.odometer_start) or "–"),
        ("Kilometerstand einde", _km(m.odometer_end) or "–"),
        ("Totaal zakelijk km", _km(m.business_km)),
        ("Te declareren", format_euro(m.amount_cents)),
    ]
    if not m.vehicle.reimbursable:
        pairs.append(("Let op", "voertuig van de BV: geen km-vergoeding"))
    pdf.pairs(pairs, 45 * MM, 90 * MM)
    pdf.space(5 * MM)

    declared = [r for r in m.rows if r.declared]
    if declared:
        rows = []
        for r in declared:
            has_km = bool(r.business_km or r.driven_km)
            rows.append(
                [
                    _date(r.date),
                    WEEKDAYS[r.date.weekday()],
                    r.description,
                    _km(r.odometer_from),
                    _km(r.odometer_to),
                    _km(r.driven_km) if has_km else "",
                    _km(r.business_km) if has_km else "",
                    _km(r.private_km) if has_km else "",
                    r.remark,
                ]
            )
        pdf.table(
            [
                "Datum",
                "Dag",
                "Omschrijving",
                "Km-stand vertrek",
                "Km-stand aankomst",
                "Gereden km",
                "Zakelijk km",
                "Privé km",
                "Opmerking",
            ],
            rows,
            _columns(20, 9, 82, 22, 22, 17, 17, 15),
            numeric=(3, 4, 5, 6, 7),
            highlight=tuple(i for i, r in enumerate(declared) if r.open_confirmation),
            total=["", "", "Totaal", "", "", "", _km(m.business_km), "", ""],
        )
    else:
        pdf.paragraph("Geen zakelijke ritten in deze maand.", gray=GRAY)

    if m.open_confirmations:
        pdf.space(4 * MM)
        pdf.ensure_room(40)  # the heading stays with its first line
        pdf.paragraph(
            "Openstaande bevestigingen (tellen mee met de conservatieve default)",
            bold=True,
            size=10,
        )
        for r in m.open_confirmations:
            pdf.paragraph(f"{_date(r.date)}: {r.description}, {_km(r.business_km)} km zakelijk")

    trips = [t for t in m.trips if t.declared]
    if with_trips and trips:
        pdf.new_page()
        pdf.paragraph(f"Ritten {MONTHS[m.month]} {m.year}", bold=True, size=TITLE_SIZE)
        pdf.paragraph(
            "Vertrokken na = laatste meting waarin de auto nog stond; het echte vertrek ligt "
            "tussen dat moment en de aankomst. Telt niet anders dan de maandstaat.",
            gray=GRAY,
            after=3 * MM,
        )
        pdf.table(
            [
                "Datum",
                "Dag",
                "Vertrokken na",
                "Aangekomen",
                "Rit",
                "Uitleg",
                "Km-stand vertrek",
                "Km-stand aankomst",
                "Gereden km",
                "Zakelijk km",
                "Privé km",
                "Opmerking",
            ],
            [
                [
                    _date(t.date),
                    WEEKDAYS[t.date.weekday()],
                    clock(t.departed_after, t.date),
                    clock(t.arrived_at, t.date),
                    t.description,
                    t.explanation,
                    _km(t.odometer_from),
                    _km(t.odometer_to),
                    _km(t.driven_km),
                    _km(t.business_km),
                    _km(t.private_km),
                    t.remark,
                ]
                for t in trips
            ],
            _columns(18, 8, 21, 21, 42, 56, 18, 18, 15, 15, 13),
            numeric=(6, 7, 8, 9, 10),
            highlight=tuple(i for i, t in enumerate(trips) if t.open_confirmation),
            total=[
                "",
                "",
                "",
                "",
                "Totaal",
                "",
                "",
                "",
                _km(sum(t.driven_km for t in trips)),
                _km(sum(t.business_km for t in trips)),
                _km(sum(t.private_km for t in trips)),
                "",
            ],
            size=SMALL,
        )


def write_maandstaat_pdf(
    m: Maandstaat, out_path: str | Path, generated_at: datetime | None = None
) -> Path:
    """The maandstaat and its trips in one PDF."""
    pdf = Pdf()
    _maandstaat(pdf, m)
    footer = f"{m.title} — {_vehicle(m.vehicle)}"
    return pdf.save(out_path, m.title, footer, CREATOR, generated_at or datetime.now(AMSTERDAM))


def write_jaaroverzicht_pdf(
    year: int,
    vehicle: Vehicle,
    maandstaten: list[Maandstaat],
    out_path: str | Path,
    generated_at: datetime | None = None,
) -> Path:
    """The overview first, then every month on its own page (without the trips, like the
    Excel)."""
    months = sorted(maandstaten, key=lambda m: m.month)
    totals = build_jaaroverzicht(months)
    title = f"Jaaroverzicht {year}"
    footer = f"{title} — {_vehicle(vehicle)}"
    pdf = Pdf()
    pdf.paragraph(footer, bold=True, size=TITLE_SIZE, after=5 * MM)
    pdf.table(
        ["Maand", "Ritdagen", "Zakelijk km", "Bedrag"],
        [
            [MONTHS[t.month], str(t.trip_days), _km(t.business_km), format_euro(t.amount_cents)]
            for t in totals
        ],
        [w * MM for w in (35, 25, 30, 30)],
        numeric=(1, 2, 3),
        total=[
            "Totaal",
            str(sum(t.trip_days for t in totals)),
            _km(sum(t.business_km for t in totals)),
            format_euro(sum(t.amount_cents for t in totals)),
        ],
    )
    for m in months:
        pdf.new_page()
        _maandstaat(pdf, m, with_trips=False)
    return pdf.save(out_path, title, footer, CREATOR, generated_at or datetime.now(AMSTERDAM))
