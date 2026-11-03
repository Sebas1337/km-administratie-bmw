"""Maandstaat and jaaroverzicht as PDF, next to the Excel (ONTWERP §9).

Same content as the Excel, for bookkeeping software (Exact Online and the like) that takes a
PDF as the attachment of a booking and for the archive: the declaration per day, the totals and
the open confirmations, then every declared trip on its own row. Built from the same
`Maandstaat` as the Excel, so both always agree. Every page has a footer with the vehicle,
version, creation date and "pagina x van y".

A4 landscape with the PDF standard fonts (no embedded font files): they cover €, é and ×
(WinAnsi); the arrow in "Thuis → Klant" is taken from the Symbol font.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..derive import AMSTERDAM
from ..model import Vehicle
from ..version import VERSION
from .maandstaat import (
    MONTHS,
    WEEKDAYS,
    Maandstaat,
    MonthTotal,
    build_jaaroverzicht,
    clock,
    format_euro,
)

PAGE = landscape(A4)
MARGIN = 14 * mm
WIDTH = PAGE[0] - 2 * MARGIN - 12  # the frame has 6 pt padding on both sides

HEADER_FILL = colors.HexColor("#DDE5F0")
OPEN_FILL = colors.HexColor("#FFF2CC")
GRID = colors.HexColor("#B0B8C4")

TEXT = ParagraphStyle("text", fontName="Helvetica", fontSize=8, leading=10)
SMALL = ParagraphStyle("small", parent=TEXT, fontSize=7.5, leading=9)
HEAD = ParagraphStyle("head", parent=TEXT, fontName="Helvetica-Bold")
HEAD_RIGHT = ParagraphStyle("head_right", parent=HEAD, alignment=TA_RIGHT)
TITLE = ParagraphStyle("title", parent=TEXT, fontName="Helvetica-Bold", fontSize=15, leading=19)
SUBTITLE = ParagraphStyle("subtitle", parent=HEAD, fontSize=10, leading=13, spaceBefore=4)
NOTE = ParagraphStyle("note", parent=TEXT, textColor=colors.HexColor("#555555"))


def _p(text: str | None, style: ParagraphStyle = TEXT) -> Paragraph:
    """Paragraph from plain text: escaped, with the arrow from the Symbol font."""
    markup = escape(text or "").replace("→", '<font face="Symbol">→</font>')
    return Paragraph(markup, style)


def _km(km: int | None) -> str:
    return "" if km is None else f"{km:,}".replace(",", ".")


def _date(d: date) -> str:
    return f"{d:%d-%m-%Y}"


def _table(
    header: list[str],
    rows: list[list],
    widths_mm: list[float],
    *,
    numeric: list[int],
    highlight: list[int] = (),
    total: list | None = None,
) -> Table:
    """Table with a repeated header row, right-aligned numeric columns, highlighted (open)
    rows and an optional bold totals row."""
    data = [[_p(h, HEAD_RIGHT if i in numeric else HEAD) for i, h in enumerate(header)], *rows]
    if total is not None:
        data.append(total)
    widths = [w * mm for w in widths_mm]
    if not widths_mm[-1]:  # 0: the last column takes the rest of the page
        widths[-1] = WIDTH - sum(widths[:-1])
    style = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, GRID),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, GRID),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    for col in numeric:
        style.append(("ALIGN", (col, 1), (col, -1), "RIGHT"))
    for row in highlight:
        style.append(("BACKGROUND", (0, row + 1), (-1, row + 1), OPEN_FILL))
    if total is not None:
        style += [
            ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 8),
            ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
        ]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle(style))
    return table


def _meta(pairs: list[tuple[str, str]]) -> Table:
    table = Table(
        [[_p(label, HEAD), _p(value)] for label, value in pairs],
        colWidths=[45 * mm, 90 * mm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    return table


def _vehicle(vehicle: Vehicle) -> str:
    return f"{vehicle.name} ({vehicle.plate})"


def maandstaat_story(m: Maandstaat, with_trips: bool = True) -> list:
    """The maandstaat (declaration per day) and, with `with_trips`, its trips on a new page."""
    story: list = [_p(m.title, TITLE), Spacer(0, 3 * mm)]
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
    story += [_meta(pairs), Spacer(0, 5 * mm)]

    declared = [r for r in m.rows if r.declared]
    rows, highlight = [], []
    for i, r in enumerate(declared):
        has_km = bool(r.business_km or r.driven_km)
        rows.append(
            [
                _date(r.date),
                WEEKDAYS[r.date.weekday()],
                _p(r.description),
                _km(r.odometer_from),
                _km(r.odometer_to),
                _km(r.driven_km) if has_km else "",
                _km(r.business_km) if has_km else "",
                _km(r.private_km) if has_km else "",
                _p(r.remark),
            ]
        )
        if r.open_confirmation:
            highlight.append(i)
    if rows:
        total = ["", "", "Totaal", "", "", "", _km(m.business_km), "", ""]
        story.append(
            _table(
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
                [20, 9, 82, 22, 22, 17, 17, 15, 0],
                numeric=[3, 4, 5, 6, 7],
                highlight=highlight,
                total=total,
            )
        )
    else:
        story.append(_p("Geen zakelijke ritten in deze maand.", NOTE))

    if m.open_confirmations:
        lines = [
            _p("Openstaande bevestigingen (tellen mee met de conservatieve default)", SUBTITLE)
        ]
        for r in m.open_confirmations:
            lines.append(_p(f"{_date(r.date)}: {r.description}, {_km(r.business_km)} km zakelijk"))
        story += [Spacer(0, 4 * mm), KeepTogether(lines)]

    trips = [t for t in m.trips if t.declared]
    if with_trips and trips:
        story += [
            PageBreak(),
            _p(f"Ritten {MONTHS[m.month]} {m.year}", TITLE),
            Spacer(0, 2 * mm),
            _p(
                "Vertrokken na = laatste meting waarin de auto nog stond; het echte vertrek ligt "
                "tussen dat moment en de aankomst. Telt niet anders dan de maandstaat.",
                NOTE,
            ),
            Spacer(0, 3 * mm),
        ]
        rows = [
            [
                _date(t.date),
                WEEKDAYS[t.date.weekday()],
                _p(clock(t.departed_after, t.date), SMALL),
                _p(clock(t.arrived_at, t.date), SMALL),
                _p(t.description, SMALL),
                _p(t.explanation, SMALL),
                _km(t.odometer_from),
                _km(t.odometer_to),
                _km(t.driven_km),
                _km(t.business_km),
                _km(t.private_km),
                _p(t.remark, SMALL),
            ]
            for t in trips
        ]
        total = [
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
        ]
        story.append(
            _table(
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
                rows,
                [18, 8, 21, 21, 42, 56, 18, 18, 15, 15, 13, 0],
                numeric=[6, 7, 8, 9, 10],
                highlight=[i for i, t in enumerate(trips) if t.open_confirmation],
                total=total,
            )
        )
    return story


def jaaroverzicht_story(year: int, vehicle: Vehicle, totals: list[MonthTotal]) -> list:
    rows = [
        [MONTHS[t.month], str(t.trip_days), _km(t.business_km), format_euro(t.amount_cents)]
        for t in totals
    ]
    total = [
        "Totaal",
        str(sum(t.trip_days for t in totals)),
        _km(sum(t.business_km for t in totals)),
        format_euro(sum(t.amount_cents for t in totals)),
    ]
    table = _table(
        ["Maand", "Ritdagen", "Zakelijk km", "Bedrag"],
        rows,
        [35, 25, 30, 30],
        numeric=[1, 2, 3],
        total=total,
    )
    table.hAlign = "LEFT"
    return [
        _p(f"Jaaroverzicht {year} — {_vehicle(vehicle)}", TITLE),
        Spacer(0, 5 * mm),
        table,
    ]


def _footer_canvas(footer: str, generated_at: datetime):
    """Canvas class that writes the footer with "pagina x van y" (known after the last page)."""
    made = f"gemaakt {generated_at.astimezone(AMSTERDAM):%d-%m-%Y %H:%M} door km-admin {VERSION}"

    class FooterCanvas(Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages: list[dict] = []

        def showPage(self):  # noqa: N802 (reportlab API)
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            for page in self._pages:
                self.__dict__.update(page)
                self.setFont("Helvetica", 7)
                self.setFillColor(colors.HexColor("#555555"))
                y = MARGIN / 2
                self.drawString(MARGIN, y, footer)
                self.drawCentredString(PAGE[0] / 2, y, made)
                self.drawRightString(
                    PAGE[0] - MARGIN, y, f"pagina {self._pageNumber} van {len(self._pages)}"
                )
                super().showPage()
            super().save()

    return FooterCanvas


def _build(story: list, out_path: str | Path, title: str, footer: str, generated_at) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(path),
        pagesize=PAGE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=title,
        author="km-admin",
        subject="Kilometeradministratie",
        creator=f"km-admin {VERSION}",
    )
    doc.build(story, canvasmaker=_footer_canvas(footer, generated_at or datetime.now(AMSTERDAM)))
    return path


def write_maandstaat_pdf(
    m: Maandstaat, out_path: str | Path, generated_at: datetime | None = None
) -> Path:
    """The maandstaat and its trips in one PDF."""
    return _build(
        maandstaat_story(m),
        out_path,
        m.title,
        f"{m.title} — {_vehicle(m.vehicle)}",
        generated_at,
    )


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
    story = jaaroverzicht_story(year, vehicle, build_jaaroverzicht(months))
    for m in months:
        story += [PageBreak(), *maandstaat_story(m, with_trips=False)]
    title = f"Jaaroverzicht {year}"
    return _build(story, out_path, title, f"{title} — {_vehicle(vehicle)}", generated_at)
