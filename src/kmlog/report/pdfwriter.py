"""A small PDF writer for the reports: text, lines and tables on A4 landscape pages.

Standard library only, so the Lambda package needs no PDF library (ONTWERP §9, decision log). It
uses the PDF standard fonts, which are not embedded: Helvetica in WinAnsi, the arrow from Symbol.
`clean` brings all text within those characters, so nothing else ever reaches a page.

A table row that does not fit moves to the next page, under a repeated header; a row taller than
a whole page continues there, so no text can make a report fail.
"""

from __future__ import annotations

import unicodedata
import zlib
from datetime import datetime
from pathlib import Path

from ..derive import AMSTERDAM

MM = 72 / 25.4
PAGE_W, PAGE_H = 297 * MM, 210 * MM  # A4 landscape
MARGIN = 14 * MM
WIDTH = PAGE_W - 2 * MARGIN
TOP, BOTTOM = PAGE_H - MARGIN, MARGIN
FOOTER_SIZE = 7
CELL_X, CELL_Y = 3, 2.5  # padding in a table cell
LEADING = 1.25

BLACK = (0, 0, 0)
GRAY = 0.33
HEADER_FILL = (0.867, 0.898, 0.941)
OPEN_FILL = (1, 0.949, 0.8)
GRID = (0.69, 0.722, 0.769)

ARROW = "→"
ARROW_WIDTH, ARROW_BYTE = 987, b"\xae"  # Symbol: arrowright

# Widths of WinAnsi 32-255 from the Adobe Core 14 AFM files.
# Copyright (c) 1985, 1987, 1989, 1990, 1997 Adobe Systems Incorporated. All Rights Reserved.
# fmt: off
_WIDTHS = (
    (
        278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556,
        556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015,
        667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778,
        722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556,
        500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
        278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 350, 556, 350, 222, 556, 333,
        1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350, 350, 222, 222, 333, 333,
        350, 556, 1000, 333, 1000, 500, 333, 944, 350, 500, 667, 278, 333, 556, 556, 556,
        556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 556,
        537, 278, 333, 333, 365, 556, 834, 834, 834, 611, 667, 667, 667, 667, 667, 667,
        1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778,
        778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889,
        500, 556, 556, 556, 556, 278, 278, 278, 278, 556, 556, 556, 556, 556, 556, 556, 584,
        611, 556, 556, 556, 556, 500, 556, 500,
    ),
    (
        278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556,
        556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722,
        722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722,
        667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556,
        611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333,
        611, 556, 778, 556, 556, 500, 389, 280, 389, 584, 350, 556, 350, 278, 556, 500,
        1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350, 350, 278, 278, 500, 500,
        350, 556, 1000, 333, 1000, 556, 333, 944, 350, 500, 667, 278, 333, 556, 556, 556,
        556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 611,
        556, 278, 333, 333, 365, 556, 834, 834, 834, 611, 722, 722, 722, 722, 722, 722,
        1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778,
        778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889,
        556, 556, 556, 556, 556, 278, 278, 278, 278, 611, 611, 611, 611, 611, 611, 611, 584,
        611, 611, 611, 611, 611, 556, 611, 556,
    ),
)
# fmt: on


def _char_widths(widths: tuple[int, ...]) -> dict[str, int]:
    chars = {ARROW: ARROW_WIDTH}
    for byte, width in enumerate(widths, 32):
        try:
            char = bytes([byte]).decode("cp1252")
        except UnicodeDecodeError:  # the few bytes WinAnsi leaves undefined
            continue
        if not unicodedata.category(char).startswith("C"):
            chars[char] = width
    return chars


_REGULAR, _BOLD = _char_widths(_WIDTHS[0]), _char_widths(_WIDTHS[1])
_SUBSTITUTES = {
    **dict(zip("ŁłĐđĦħŦŧŊŋı", "LlDdHhTtNni", strict=True)),  # no decomposition below
    "−": "-",
    "‐": "-",
    "‑": "-",
    "≤": "<=",
    "≥": ">=",
}
_DROPPED = {"So", "Sk", "Mn", "Me", "Cf", "Cs", "Co", "Cn"}  # emoji, accents on their own, ...


def _replacement(char: str) -> str:
    if char in _SUBSTITUTES:
        return _SUBSTITUTES[char]
    base = "".join(c for c in unicodedata.normalize("NFKD", char) if c in _REGULAR)
    if base:  # ň -> n, ĳ -> ij
        return base
    category = unicodedata.category(char)
    if category == "Cc" or category.startswith("Z"):
        return " "
    return "" if category in _DROPPED else "?"  # another alphabet: visibly incomplete


def clean(text: str | None) -> str:
    """The text with only characters the PDF can show. Letters become their closest plain letter,
    emoji disappear, and anything else that has no equivalent becomes "?"."""
    return "".join(c if c in _REGULAR else _replacement(c) for c in text or "")


def _width(cleaned: str, bold: bool, size: float) -> float:
    chars = _BOLD if bold else _REGULAR
    return sum(chars[c] for c in cleaned) * size / 1000


def text_width(text: str | None, bold: bool = False, size: float = 8) -> float:
    return _width(clean(text), bold, size)


def _split_word(word: str, width: float, bold: bool, size: float) -> list[str]:
    """A word wider than the column, in pieces that fit."""
    chars = _BOLD if bold else _REGULAR
    limit = width * 1000 / size
    pieces, start, used = [], 0, 0
    for i, c in enumerate(word):
        if used + chars[c] > limit and i > start:
            pieces.append(word[start:i])
            start, used = i, 0
        used += chars[c]
    return [*pieces, word[start:]]


def wrap(text: str | None, width: float, bold: bool = False, size: float = 8) -> list[str]:
    """The text as lines of at most `width` points; always at least one line."""
    lines, line, used = [], "", 0.0
    space = _width(" ", bold, size)
    for word in clean(text).split():
        word_width = _width(word, bold, size)
        if word_width > width:
            *pieces, word = _split_word(word, width, bold, size)
            if line:
                lines.append(line)
            lines += pieces
            line, used = "", 0.0
            word_width = _width(word, bold, size)
        if line and used + space + word_width > width:
            lines.append(line)
            line, used = "", 0.0
        line = f"{line} {word}" if line else word
        used += word_width + (space if used else 0)
    return [*lines, line] if line or not lines else lines


def _fit(text: str, width: float, size: float) -> str:
    """The text cut off with "…" if it is wider than `width`."""
    cleaned = clean(text)
    if _width(cleaned, False, size) <= width:
        return cleaned
    return _split_word(cleaned, width - _width("…", False, size), False, size)[0].rstrip() + "…"


def _pdf_date(moment: datetime) -> str:
    offset = moment.strftime("%z")
    return f"D:{moment:%Y%m%d%H%M%S}{offset[:3]}'{offset[3:5]}'"


def _info_text(text: str) -> str:
    return f"<feff{text.encode('utf-16-be').hex()}>"


class Pdf:
    """Pages of drawing operations; `y` is where the next content starts, from the bottom."""

    def __init__(self) -> None:
        self.pages: list[list[str]] = []
        self.new_page()

    def new_page(self) -> None:
        self._ops: list[str] = []
        self.pages.append(self._ops)
        self.y = TOP

    def ensure_room(self, height: float) -> None:
        if self.y - height < BOTTOM:
            self.new_page()

    def space(self, height: float) -> None:
        self.y -= height

    def text(
        self, x: float, y: float, text: str | None, bold: bool = False, size=8, gray=0
    ) -> None:
        font = "/F2" if bold else "/F1"
        ops = [f"{gray} g BT {x:.2f} {y:.2f} Td"]
        for i, part in enumerate(clean(text).split(ARROW)):
            if i:
                ops.append(f"/F3 {size} Tf <{ARROW_BYTE.hex()}> Tj")
            if part:
                ops.append(f"{font} {size} Tf <{part.encode('cp1252').hex()}> Tj")
        self._ops.append(" ".join(ops) + " ET 0 g")

    def rect(self, x: float, y: float, width: float, height: float, rgb: tuple) -> None:
        self._ops.append(
            f"{rgb[0]} {rgb[1]} {rgb[2]} rg {x:.2f} {y:.2f} {width:.2f} {height:.2f} re f 0 g"
        )

    def line(self, x1: float, x2: float, y: float, weight=0.25, rgb: tuple = GRID) -> None:
        self._ops.append(
            f"{rgb[0]} {rgb[1]} {rgb[2]} RG {weight} w {x1:.2f} {y:.2f} m {x2:.2f} {y:.2f} l S"
        )

    def paragraph(self, text: str | None, bold=False, size=8, gray=0, after=2) -> None:
        for line in wrap(text, WIDTH, bold, size):
            self.ensure_room(size * LEADING)
            self.y -= size * LEADING
            self.text(MARGIN, self.y, line, bold, size, gray)
        self.y -= after

    def pairs(self, pairs: list[tuple[str, str]], label_width: float, value_width: float) -> None:
        """Label (bold) and value per row, as at the top of a statement."""
        lead = 8 * LEADING + 2
        for label, value in pairs:
            for i, line in enumerate(wrap(value, value_width)):
                self.y -= lead
                if not i:
                    self.text(MARGIN, self.y, label, bold=True)
                self.text(MARGIN + label_width, self.y, line)

    def table(
        self,
        header: list[str],
        rows: list[list[str | None]],
        widths: list[float],
        *,
        numeric: tuple[int, ...] = (),
        highlight: tuple[int, ...] = (),
        total: list[str] | None = None,
        size: float = 8,
    ) -> None:
        """Table with a header on every page, right-aligned `numeric` columns, a fill on the
        `highlight` rows and an optional bold `total` row."""
        lead = size * LEADING
        xs = [MARGIN]
        for width in widths:
            xs.append(xs[-1] + width)

        def cells(row: list[str | None], bold: bool) -> list[list[str]]:
            return [
                wrap(cell, width - 2 * CELL_X, bold, size)
                for cell, width in zip(row, widths, strict=True)
            ]

        def height(lines: list[list[str]]) -> float:
            return max(len(cell) for cell in lines) * lead + 2 * CELL_Y

        def draw(lines: list[list[str]], bold: bool = False, fill: tuple | None = None) -> None:
            row_height = height(lines)
            if fill:
                self.rect(xs[0], self.y - row_height, xs[-1] - xs[0], row_height - 0.3, fill)
            for col, cell in enumerate(lines):
                y = self.y - CELL_Y
                for line in cell:
                    y -= lead
                    x = xs[col] + CELL_X
                    if col in numeric:
                        x = xs[col + 1] - CELL_X - text_width(line, bold, size)
                    self.text(x, y + 2, line, bold, size)
            self.y -= row_height
            self.line(xs[0], xs[-1], self.y)

        head = cells(header, True)
        full_page = TOP - BOTTOM - height(head)
        self.ensure_room(height(head) + lead + 2 * CELL_Y)
        draw(head, True, HEADER_FILL)
        for i, row in enumerate([*rows, total] if total else rows):
            is_total = total is not None and i == len(rows)
            fill = OPEN_FILL if i in highlight else None
            lines = cells(row, is_total)
            while height(lines) > self.y - BOTTOM:
                fit = int((self.y - BOTTOM - 2 * CELL_Y) / lead)
                if height(lines) > full_page and fit >= 1:  # never fits: split it over the pages
                    draw([cell[:fit] for cell in lines], is_total, fill)
                    lines = [cell[fit:] for cell in lines]
                self.new_page()
                draw(head, True, HEADER_FILL)
            if is_total:
                self.line(xs[0], xs[-1], self.y, 0.8, BLACK)
            draw(lines, is_total, fill)

    def _footer(self, ops: list[str], number: int, footer: str, made: str) -> None:
        self._ops = ops
        y = MARGIN / 2
        made_width = text_width(made, size=FOOTER_SIZE)
        made_x = (PAGE_W - made_width) / 2
        left = _fit(footer, made_x - MARGIN - 12, FOOTER_SIZE)
        self.text(MARGIN, y, left, size=FOOTER_SIZE, gray=GRAY)
        self.text(made_x, y, made, size=FOOTER_SIZE, gray=GRAY)
        right = f"pagina {number} van {len(self.pages)}"
        right_x = PAGE_W - MARGIN - text_width(right, size=FOOTER_SIZE)
        self.text(right_x, y, right, size=FOOTER_SIZE, gray=GRAY)

    def save(
        self, out_path: str | Path, title: str, footer: str, creator: str, generated_at: datetime
    ) -> Path:
        """Write the file, with on every page the footer, the moment and "pagina x van y"."""
        local = generated_at.astimezone(AMSTERDAM)
        made = f"gemaakt {local:%d-%m-%Y %H:%M} door {creator}"
        # 1 catalog, 2 page tree, 3-5 fonts, 6 document info, then per page a page and its content
        first_page = 7
        kids = " ".join(f"{first_page + 2 * i} 0 R" for i in range(len(self.pages)))
        objects = [
            b"<</Type/Catalog/Pages 2 0 R>>",
            f"<</Type/Pages/Count {len(self.pages)}/Kids[{kids}]>>".encode(),
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica-Bold/Encoding/WinAnsiEncoding>>",
            b"<</Type/Font/Subtype/Type1/BaseFont/Symbol>>",
            (
                f"<</Title{_info_text(title)}/Author{_info_text('km-admin')}"
                f"/Subject{_info_text('Kilometeradministratie')}/Creator{_info_text(creator)}"
                f"/CreationDate({_pdf_date(local)})>>"
            ).encode(),
        ]
        for i, ops in enumerate(self.pages):
            self._footer(ops, i + 1, footer, made)
            content = zlib.compress("\n".join(ops).encode("ascii"))
            objects += [
                (
                    f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {PAGE_W:.2f} {PAGE_H:.2f}]"
                    "/Resources<</Font<</F1 3 0 R/F2 4 0 R/F3 5 0 R>>>>"
                    f"/Contents {first_page + 2 * i + 1} 0 R>>"
                ).encode(),
                b"<</Filter/FlateDecode/Length %d>>\nstream\n%b\nendstream"
                % (len(content), content),
            ]
        data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, 1):
            offsets.append(len(data))
            data += b"%d 0 obj\n%b\nendobj\n" % (number, body)
        xref = len(data)
        data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        data += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
        data += (
            f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R/Info 6 0 R>>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path
