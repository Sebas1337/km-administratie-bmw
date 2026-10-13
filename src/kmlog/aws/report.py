"""Monthly report: maandstaat of the previous month (+ jaaroverzicht to date) -> S3 + SES mail.

Invoked by EventBridge Scheduler on the 1st at 06:00 Europe/Amsterdam. The event may carry
{"year": 2026, "month": 9} to (re)build a specific month; {"send": false} skips the mail.
The jaaroverzicht is always refreshed in S3 but only attached to the last mail of the year
(december), unless the event says {"jaaroverzicht": true}.
Files: s3://$REPORTS_BUCKET/reports/<year>/<year>-<mm>.xlsx and .../jaaroverzicht-<year>.xlsx
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from ..derive import AMSTERDAM
from ..report.maandstaat import format_euro as euro
from ..report.maandstaat import write_jaaroverzicht, write_maandstaat
from ..report.service import maandstaat_from_store, year_to_date
from ..store import Store
from . import env, log_event, mail_subject

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def previous_month(now: datetime) -> tuple[int, int]:
    local = now.astimezone(AMSTERDAM)
    return (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)


def build_mail(sender: str, to: str, subject: str, body: str, files: list[Path]) -> bytes:
    msg = MIMEMultipart()
    msg["From"], msg["To"], msg["Subject"] = sender, to, mail_subject(subject)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    for f in files:
        part = MIMEApplication(f.read_bytes(), _subtype=XLSX.split("/")[1])
        part.add_header("Content-Disposition", "attachment", filename=f.name)
        msg.attach(part)
    return msg.as_bytes()


def run(
    store: Store,
    vehicle: str,
    year: int,
    month: int,
    s3,
    bucket: str,
    ses,
    email: str | None,
    attach_jaaroverzicht: bool | None = None,
):
    if attach_jaaroverzicht is None:
        attach_jaaroverzicht = month == 12
    m = maandstaat_from_store(store, vehicle, year, month)
    months = year_to_date(store, vehicle, year, month)
    with tempfile.TemporaryDirectory() as tmp:
        files = [
            write_maandstaat(m, Path(tmp) / f"{year}-{month:02d}.xlsx"),
            write_jaaroverzicht(year, m.vehicle, months, Path(tmp) / f"jaaroverzicht-{year}.xlsx"),
        ]
        keys = []
        for f in files:
            key = f"reports/{year}/{f.name}"
            s3.upload_file(str(f), bucket, key, ExtraArgs={"ContentType": XLSX})
            keys.append(key)
        if email:
            body = (
                f"{m.title}: {m.business_km} km × {euro(m.cents_per_km)} = "
                f"{euro(m.amount_cents)}.\n"
                + (
                    f"{len(m.open_confirmations)} dag(en) met openstaande bevestiging "
                    "(tellen mee met de conservatieve default).\n"
                    if m.open_confirmations
                    else ""
                )
                + f"\nJaar {year} tot en met deze maand: "
                f"{sum(x.business_km for x in months)} km = "
                f"{euro(sum(x.amount_cents for x in months))}.\n"
            )
            attachments = files if attach_jaaroverzicht else files[:1]
            raw = build_mail(email, email, m.title, body, attachments)
            ses.send_raw_email(Source=email, Destinations=[email], RawMessage={"Data": raw})
    return {
        "event": "report",
        "year": year,
        "month": month,
        "business_km": m.business_km,
        "amount_cents": m.amount_cents,
        "open_confirmations": len(m.open_confirmations),
        "keys": keys,
        "mailed": bool(email),
        "jaaroverzicht_attached": bool(email) and attach_jaaroverzicht,
    }


def handler(event, context, *, store: Store | None = None, s3=None, ses=None) -> dict:
    import boto3

    from ..store.dynamodb import DynamoDbStore

    event = event or {}
    year, month = previous_month(datetime.now(AMSTERDAM))
    year, month = int(event.get("year", year)), int(event.get("month", month))
    store = store or DynamoDbStore(env("TABLE_NAME", "kmlog-data"))
    result = run(
        store,
        env("VEHICLE_ID", "car"),
        year,
        month,
        s3 or boto3.client("s3"),
        env("REPORTS_BUCKET"),
        ses or boto3.client("ses"),
        env("REPORT_EMAIL") if event.get("send", True) else None,
        event.get("jaaroverzicht"),
    )
    log_event(**result)
    return result
