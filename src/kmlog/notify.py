"""Daily notification (ONTWERP §9): one mail with the new open questions.

Run once a day (20:00 Europe/Amsterdam):
1. Re-derive the last WINDOW_DAYS days: this re-applies answers and marks questions older than
   CONFIRM_TIMEOUT_DAYS as TIMED_OUT (the conservative default stays).
2. Collect open questions not mailed before, plus rule 7 ("gemist?": a workday with driven km
   but no business km) for days not mailed before, if `Settings.mail_missed_days` is on.
3. Nothing new -> no mail. Otherwise one mail with a link to the web UI; remember what was sent.
   With the setting off, "gemist?" days are remembered without a mail, so switching it on later
   does not mail the past month in one go.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .actions import OpenQuestion, open_questions
from .classify import missed_business_days
from .derive import amsterdam_date
from .process import reprocess
from .report.maandstaat import WEEKDAYS, describe
from .store import Store

WINDOW_DAYS = 31


@dataclass
class Digest:
    questions: list[OpenQuestion] = field(default_factory=list)
    missed: list = field(default_factory=list)  # dates

    @property
    def empty(self) -> bool:
        return not self.questions and not self.missed

    @property
    def keys(self) -> list[str]:
        return [f"q:{q.segment_id}" for q in self.questions] + [
            f"missed:{d.isoformat()}" for d in self.missed
        ]


def collect(store: Store, vehicle: str, now: datetime) -> Digest:
    today = amsterdam_date(now)
    first = today - timedelta(days=WINDOW_DAYS)
    reprocess(store, vehicle, first, today, now)
    legs = store.legs(vehicle, first, today)
    seen = store.notified(vehicle)
    missed = [d for d in missed_business_days(legs) if f"missed:{d.isoformat()}" not in seen]
    if missed and not store.settings().mail_missed_days:
        store.mark_notified(vehicle, [f"missed:{d.isoformat()}" for d in missed], now)
        missed = []
    return Digest(
        questions=[q for q in open_questions(legs) if f"q:{q.segment_id}" not in seen],
        missed=missed,
    )


def render(digest: Digest, store: Store, web_url: str | None) -> tuple[str, str]:
    names = {loc.id: loc.name for loc in store.locations()}
    lines = []
    if digest.questions:
        lines.append("Openstaande vragen:")
        for q in digest.questions:
            d = q.date
            lines.append(
                f"- {WEEKDAYS[d.weekday()]} {d:%d-%m}: {describe(q.legs, names)}, "
                f"{q.distance_km} km gereden (nu {q.business_km} km zakelijk)"
            )
        lines.append("")
        lines.append(
            "Zonder antwoord blijft na 7 dagen de conservatieve standaard staan (niet meer "
            "declareren dan zeker zakelijk is)."
        )
    if digest.missed:
        lines.append("")
        lines.append("Gereden op een werkdag zonder zakelijke rit (gemist?):")
        for d in digest.missed:
            lines.append(f"- {WEEKDAYS[d.weekday()]} {d:%d-%m}")
    if web_url:
        lines += ["", f"Beantwoorden of corrigeren: {web_url.rstrip('/')}/"]
    n = len(digest.questions)
    subject = (
        f"Kilometeradministratie: {n} {'vraag' if n == 1 else 'vragen'}"
        if n
        else "Kilometeradministratie: gemiste rit?"
    )
    return subject, "\n".join(lines).strip() + "\n"


def run(store: Store, vehicle: str, now: datetime, send, web_url: str | None) -> dict:
    """`send(subject, body)` delivers the mail; returns a summary for the log."""
    digest = collect(store, vehicle, now)
    if not digest.empty:
        subject, body = render(digest, store, web_url)
        send(subject, body)
        store.mark_notified(vehicle, digest.keys, now)
    return {
        "event": "notify",
        "questions": len(digest.questions),
        "missed_days": len(digest.missed),
        "mailed": not digest.empty,
    }
