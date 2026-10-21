"""Command line: `uv run kmlog --help`.

    kmlog stamdata                     <- data/stamdata.toml (vehicle, locations, routes, rates)
    kmlog maandstaat 2026 9            -> data/reports/2026/2026-09.xlsx
    kmlog jaaroverzicht 2026           -> data/reports/2026/jaaroverzicht-2026.xlsx

Everything lives in a local SQLite database (default data/kmlog.sqlite, gitignored), or with
`--store dynamodb` in the AWS table (default kmlog-data; AWS credentials from the environment):

    kmlog --store dynamodb stamdata
    kmlog --store dynamodb maandstaat 2026 10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import stamdata
from .report.maandstaat import (
    MONTHS,
    Maandstaat,
    build_jaaroverzicht,
    format_euro,
    write_jaaroverzicht,
    write_maandstaat,
)
from .report.service import MissingConfig, maandstaat_from_store
from .store import Store, open_store
from .version import VERSION

DEFAULT_DB = Path("data/kmlog.sqlite")
DEFAULT_REPORTS = Path("data/reports")
DEFAULT_VEHICLE = "car"  # key for observations/legs; same as VEHICLE_ID in AWS


def _store_stamdata(store: Store, vehicle_id: str) -> stamdata.Stamdata:
    return stamdata.Stamdata(
        store.locations(), store.routes(), store.rates(), store.vehicle(vehicle_id)
    )


def cmd_stamdata(args, store: Store) -> None:
    if args.export is not None:
        text = stamdata.dumps(_store_stamdata(store, args.vehicle))
        if args.export == "-":
            print(text)
        else:
            Path(args.export).write_text(text)
            print(f"stamdata uit {args.target} → {args.export}")
        return
    data = stamdata.load(args.path, args.vehicle)
    current = _store_stamdata(store, args.vehicle)
    has_places = bool(current.locations or current.routes)
    if has_places and not stamdata.same_places(current, data) and not args.force:
        sys.exit(
            f"De stamdata in {args.target} wijkt af van {args.path} (bijv. gewijzigd via de "
            "webinterface). Exporteer eerst met `kmlog stamdata --export <bestand>` en vergelijk, "
            "of overschrijf bewust met --force."
        )
    store.replace_places(data.locations, data.routes)
    if data.vehicle:
        store.put_vehicle(data.vehicle)
    for rate in data.rates:
        store.put_rate(rate)
    print(
        f"{len(data.locations)} locaties, {len(data.routes)} routes, {len(data.rates)} tarieven "
        f"uit {args.path} → {args.target}"
    )


def _maandstaat(store: Store, vehicle_id: str, year: int, month: int) -> Maandstaat:
    try:
        return maandstaat_from_store(store, vehicle_id, year, month)
    except MissingConfig as e:
        sys.exit(str(e))


def cmd_maandstaat(args, store: Store) -> None:
    m = _maandstaat(store, args.vehicle, args.year, args.month)
    out = args.out or DEFAULT_REPORTS / str(args.year) / f"{args.year}-{args.month:02d}.xlsx"
    write_maandstaat(m, out)
    print(f"{m.title}: {m.business_km} km = {format_euro(m.amount_cents)} → {out}")
    if m.open_confirmations:
        print(f"  {len(m.open_confirmations)} dag(en) met openstaande bevestiging")


def cmd_jaaroverzicht(args, store: Store) -> None:
    months = [_maandstaat(store, args.vehicle, args.year, m) for m in range(1, 13)]
    months = [m for m in months if m.rows]
    out = args.out or DEFAULT_REPORTS / str(args.year) / f"jaaroverzicht-{args.year}.xlsx"
    write_jaaroverzicht(args.year, store.vehicle(args.vehicle), months, out)
    for t in build_jaaroverzicht(months):
        print(f"  {MONTHS[t.month]:10} {t.business_km:6} km  {format_euro(t.amount_cents):>12}")
    total_km = sum(m.business_km for m in months)
    total = format_euro(sum(m.amount_cents for m in months))
    print(f"Totaal {args.year}: {total_km} km = {total} → {out}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kmlog", description="Kilometeradministratie")
    parser.add_argument("--version", action="version", version=f"kmlog {VERSION}")
    parser.add_argument("--store", choices=["sqlite", "dynamodb"], default="sqlite")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite file")
    parser.add_argument("--table", default="kmlog-data", help="DynamoDB table")
    parser.add_argument("--vehicle", default=DEFAULT_VEHICLE)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("stamdata", help="voertuig, locaties, routes en tarieven inlezen (TOML)")
    p.add_argument("path", type=Path, nargs="?", default=stamdata.DEFAULT_PATH)
    p.add_argument(
        "--export",
        nargs="?",
        const="-",
        metavar="BESTAND",
        help="stamdata uit de database naar TOML schrijven (zonder bestand: naar stdout)",
    )
    p.add_argument(
        "--force", action="store_true", help="afwijkende stamdata in de database overschrijven"
    )
    p.set_defaults(func=cmd_stamdata)

    p = sub.add_parser("maandstaat", help="maandstaat als .xlsx")
    p.add_argument("year", type=int)
    p.add_argument("month", type=int, choices=range(1, 13), metavar="month")
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_maandstaat)

    p = sub.add_parser("jaaroverzicht", help="jaaroverzicht + alle maanden als .xlsx")
    p.add_argument("year", type=int)
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_jaaroverzicht)

    args = parser.parse_args(argv)
    args.target = args.db if args.store == "sqlite" else f"dynamodb:{args.table}"
    store = open_store(args.store, path=args.db, table=args.table)
    try:
        args.func(args, store)
    finally:
        store.close()


if __name__ == "__main__":
    main()
