"""Daily notification (ONTWERP §9): one mail with the new open questions.

Run once a day (20:00 Europe/Amsterdam):
1. Re-derive the last WINDOW_DAYS days: this re-applies answers and marks questions older than
   CONFIRM_TIMEOUT_DAYS as TIMED_OUT (the conservative default stays).
2. Collect open questions not mailed before, plus rule 7 ("gemist?": a workday with driven km
   but no business km) for days not mailed before, if `Settings.mail_missed_days` is on.
   Poll gaps (rule 8): while BMW is still unreachable and today (a workday) missed
   MIN_GAP_POLLS polls, ask to add today's business trips by hand; once the polls are back,
   every leg across a gap with a workday in it, with the km the car drove meanwhile. A gap
   without km gives no leg and so no mail.
3. Nothing new -> no mail. Otherwise one mail with a link to the web UI; remember what was sent.
   With the setting off, "gemist?" days are remembered without a mail, so switching it on later
   does not mail the past month in one go.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .actions import OpenQuestion, open_questions
from .classify import is_workday, missed_business_days, moment
from .derive import (
    AMSTERDAM,
    FLAG_POLL_GAP,
    MIN_GAP_POLLS,
    POLL_SLACK,
    amsterdam_date,
    gap_days,
    missed_polls,
)
from .model import Leg
from .process import reprocess
from .report.maandstaat import WEEKDAYS, describe
from .store import Store

WINDOW_DAYS = 31
OUTAGE_MAX_DAYS = 14  # no observation for longer: the car is unlinked, not an outage


@dataclass
class Digest:
    questions: list[OpenQuestion] = field(default_factory=list)
    missed: list = field(default_factory=list)  # dates
    gaps: list[Leg] = field(default_factory=list)  # legs across a poll gap that has closed
    outage_since: datetime | None = None  # BMW still unreachable today: the last observation
    today: date | None = None

    @property
    def empty(self) -> bool:
        return not self.questions and not self.missed and not self.gaps and not self.outage_since

    @property
    def keys(self) -> list[str]:
        keys = [f"q:{q.segment_id}" for q in self.questions]
        keys += [f"missed:{d.isoformat()}" for d in self.missed]
        keys += [f"gap:{leg.id}" for leg in self.gaps]
        if self.outage_since and self.today:
            keys.append(f"outage:{self.today.isoformat()}")
        return keys


def outage_since(store: Store, vehicle: str, now: datetime) -> datetime | None:
    """When BMW was last reached, if today missed MIN_GAP_POLLS polls since then (rule 8)."""
    last = store.observation_before(vehicle, now)
    if last is None or now - last.polled_at > timedelta(days=OUTAGE_MAX_DAYS):
        return None
    midnight = datetime.combine(amsterdam_date(now), time(), AMSTERDAM)
    start = max(last.polled_at, midnight)
    missed = missed_polls([last.polled_at], start, now - POLL_SLACK, store.settings())
    return last.polled_at if len(missed) >= MIN_GAP_POLLS else None


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
    gaps = [
        leg
        for leg in legs
        if FLAG_POLL_GAP in leg.flags
        and f"gap:{leg.id}" not in seen
        and any(is_workday(d) for d in gap_days(leg))
    ]
    outage = None
    if is_workday(today) and f"outage:{today.isoformat()}" not in seen:
        outage = outage_since(store, vehicle, now)
    return Digest(
        questions=[q for q in open_questions(legs) if f"q:{q.segment_id}" not in seen],
        missed=missed,
        gaps=gaps,
        outage_since=outage,
        today=today,
    )


def render(digest: Digest, store: Store, web_url: str | None) -> tuple[str, str]:
    names = {loc.id: loc.name for loc in store.locations()}
    base = web_url.rstrip("/") if web_url else None
    lines = []
    if digest.outage_since and digest.today:
        lines.append(
            f"BMW is niet bereikbaar sinds {moment(digest.outage_since)}: er zijn vandaag "
            "geen metingen. Reed je vandaag zakelijk? Voeg de rit toe op de dagpagina"
            + (f": {base}/dag/{digest.today.isoformat()}" if base else ".")
        )
        lines.append(
            "Zodra de metingen terug zijn, krijg je een mail met de kilometers die de auto in "
            "die tijd reed."
        )
        lines.append("")
    if digest.gaps:
        lines.append("Metingen ontbraken (BMW was niet bereikbaar):")
        for leg in digest.gaps:
            d = leg.date
            when = (
                f"{moment(leg.departed_after)} tot {moment(leg.arrived_at)}"
                if leg.departed_after and leg.arrived_at
                else f"{WEEKDAYS[d.weekday()]} {d:%d-%m}"
            )
            lines.append(
                f"- {when}: {int(leg.distance_km)} km gereden ({describe([leg], names)}), "
                f"nu {int(leg.business_km)} km zakelijk op {WEEKDAYS[d.weekday()]} {d:%d-%m}"
                + (f": {base}/dag/{d.isoformat()}" if base else "")
            )
        lines.append("")
        lines.append(
            "Reed je in die tijd zakelijk? Voeg je ritten toe op de dagpagina van elke dag; "
            "dan telt de rit over het gat als privé."
        )
        lines.append("")
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
    if base:
        lines += ["", f"Beantwoorden of corrigeren: {base}/"]
    n = len(digest.questions)
    if n:
        subject = f"Kilometeradministratie: {n} {'vraag' if n == 1 else 'vragen'}"
    elif digest.outage_since:
        subject = "Kilometeradministratie: BMW niet bereikbaar"
    elif digest.gaps:
        subject = "Kilometeradministratie: metingen ontbraken"
    else:
        subject = "Kilometeradministratie: gemiste rit?"
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
        "gaps": len(digest.gaps),
        "outage": digest.outage_since is not None,
        "mailed": not digest.empty,
    }
