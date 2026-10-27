"""Classification: legs -> business / private / needs-confirmation. See ONTWERP.md §7.2.

Classification works on segments rather than single legs: a segment is a run of legs between two
stops at business places (home, client, business), with zero or more stops elsewhere in between
(rule 3: client -> stop -> home). Leading/trailing legs that never reach such a place form a
segment too.

The reference for a trip between two business places is the expected distance E, learned from
earlier plausible trips between them (`Distances`); there are no configured routes any more.
Unknown places and weekends are private without a question. A leg across a poll gap (rule 8)
never asks either: it counts at most the expected distance, and nothing once the user added
manual trips for the days of the gap. Conservative default throughout
(R6): an unanswered question never increases the reimbursed amount. Test fixtures: the example
days in ONTWERP.md §7.3.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from .derive import AMSTERDAM, FLAG_POLL_GAP, distance_m, gap_days
from .model import Classification, Leg, LegStatus, Location, LocationType

CONFIRM_TIMEOUT_DAYS = 7
MARGIN = Decimal("0.15")  # a trip fits when km <= E + max(MIN_MARGIN_KM, 15% of E)
MIN_MARGIN_KM = Decimal(2)
HISTORY_TRIPS = 10  # E = most frequent km of the last 10 plausible trips between the two places
PLAUSIBLE_SLACK_KM = Decimal(2)  # odometer rounding around 1x..2x the straight-line distance

BUSINESS_TYPES = {LocationType.HOME, LocationType.CLIENT, LocationType.BUSINESS}
WORK_TYPES = {LocationType.CLIENT, LocationType.BUSINESS}

FLAG_WEEKEND_OR_HOLIDAY = "weekend_or_holiday"
FLAG_IMPLAUSIBLE = "implausible"
FLAG_NO_ROUTE = "no_route"  # before v0.3.0; still on stored legs outside the re-derive window


class Question(StrEnum):
    DETOUR_OR_PRIVATE_STOP = "detour_or_private_stop"  # rule 2
    BUSINESS_OR_PRIVATE = "business_or_private"  # rule 2: first trip that is not plausible
    WEEKEND = "weekend_or_holiday"  # rule 6: "Toch zakelijk" on a weekend or holiday


class Answer(StrEnum):
    DETOUR = "detour"  # rule 2: all km business
    PRIVATE_STOP = "private_stop"  # rule 2: keep the expected distance
    BUSINESS = "business"
    PRIVATE = "private"
    CONFIRM = "confirm"  # rule 6: "Toch zakelijk"


OPTIONS = {
    Question.DETOUR_OR_PRIVATE_STOP: (Answer.DETOUR, Answer.PRIVATE_STOP),
    Question.BUSINESS_OR_PRIVATE: (Answer.BUSINESS, Answer.PRIVATE),
    Question.WEEKEND: (Answer.CONFIRM, Answer.PRIVATE),
}


class Reason(StrEnum):
    """Why a segment got its outcome; shown to the user via `explain`."""

    FITS = "fits"  # rule 1: within E + margin
    FIRST_TRIP = "first_trip"  # rule 1: no E yet, plausible
    DETOUR = "detour"  # rule 2: longer than E + margin
    IMPLAUSIBLE = "implausible"  # rule 2: no E yet, not plausible
    STOP = "stop"  # rule 3: E between the two business places
    STOP_CROW = "stop_crow"  # rule 3 without E: straight-line distance
    NO_BUSINESS_PLACE = "no_business_place"  # rule 5
    SAME_PLACE = "same_place"  # rule 5: starts and ends at the same place
    WEEKEND = "weekend"  # rule 6
    GAP = "gap"  # rule 8: across a poll gap, longer than E + margin or no E yet
    GAP_MANUAL = "gap_manual"  # rule 8: the user added manual trips for the days of the gap


@dataclass
class Segment:
    legs: list[Leg]
    rule: int
    question: Question | None = None
    # business km if the question is answered in the business direction
    business_if_confirmed: Decimal = Decimal(0)
    reason: Reason | None = None

    @property
    def distance_km(self) -> Decimal:
        return sum((leg.distance_km for leg in self.legs), Decimal(0))

    @property
    def business_km(self) -> Decimal:
        return sum((leg.business_km for leg in self.legs), Decimal(0))


# --- calendar -----------------------------------------------------------------------------


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def dutch_holidays(year: int) -> set[date]:
    """National holidays on which one does not normally work (Bevrijdingsdag in lustrum years)."""
    e = easter(year)
    kingsday = date(year, 4, 27)
    if kingsday.weekday() == 6:
        kingsday = date(year, 4, 26)
    days = {
        date(year, 1, 1),
        e,
        e + timedelta(days=1),
        kingsday,
        e + timedelta(days=39),  # Hemelvaart
        e + timedelta(days=49),
        e + timedelta(days=50),  # Pinksteren
        date(year, 12, 25),
        date(year, 12, 26),
    }
    if year % 5 == 0:
        days.add(date(year, 5, 5))
    return days


def is_workday(d: date) -> bool:
    return d.weekday() < 5 and d not in dutch_holidays(d.year)


# --- expected distances -------------------------------------------------------------------


def _when(leg: Leg) -> datetime:
    return leg.departed_after or leg.arrived_at or datetime.min


def margin(expected_km: int) -> Decimal:
    return max(MIN_MARGIN_KM, MARGIN * expected_km)


class Distances:
    """Expected distance per pair of business places, learned from earlier plausible direct
    trips between them (both directions). Feed trips in any order with `learn`; `expected`
    only looks at trips that departed before the given moment, so a new trip never changes how
    an older one was judged."""

    def __init__(self, locations: dict[str, Location]) -> None:
        self.locations = locations
        self._trips: dict[frozenset[str], dict[str, tuple[datetime, int]]] = {}

    def business_pair(self, a: str | None, b: str | None) -> bool:
        return (
            a is not None
            and b is not None
            and a != b
            and a in self.locations
            and b in self.locations
            and self.locations[a].type in BUSINESS_TYPES
            and self.locations[b].type in BUSINESS_TYPES
        )

    def crow_km(self, a: str, b: str) -> Decimal:
        la, lb = self.locations[a], self.locations[b]
        return Decimal(distance_m(la.lat, la.lon, lb.lat, lb.lon) / 1000)

    def plausible(self, a: str, b: str, km: Decimal) -> bool:
        crow = self.crow_km(a, b)
        return crow - PLAUSIBLE_SLACK_KM <= km <= 2 * crow + PLAUSIBLE_SLACK_KM

    def learn(self, leg: Leg) -> None:
        a, b = leg.from_location, leg.to_location
        if FLAG_POLL_GAP in leg.flags:  # may hide other trips: not a direct trip
            return
        if self.business_pair(a, b) and self.plausible(a, b, leg.distance_km):
            pair = frozenset((a, b))
            self._trips.setdefault(pair, {})[leg.id] = (_when(leg), int(leg.distance_km))

    def history(self, a: str, b: str, before: datetime | None = None) -> list[int]:
        trips = self._trips.get(frozenset((a, b)), {}).values()
        trips = sorted(t for t in trips if not before or t[0] < before)
        return [km for _, km in trips[-HISTORY_TRIPS:]]

    def expected(self, a: str, b: str, before: datetime | None = None) -> int | None:
        counts = Counter(self.history(a, b, before))
        if not counts:
            return None
        top = max(counts.values())
        return min(km for km, n in counts.items() if n == top)

    def pairs(self) -> list[tuple[str, str, int, int]]:
        """(a, b, expected km, number of trips) for every pair with history, for the UI."""
        out = []
        for pair in self._trips:
            a, b = sorted(pair)
            out.append((a, b, self.expected(a, b) or 0, len(self.history(a, b))))
        return sorted(out)


# --- classification -----------------------------------------------------------------------


def _set(leg: Leg, business: Decimal, status: LegStatus = LegStatus.AUTO) -> None:
    business = max(Decimal(0), min(business, leg.distance_km))
    leg.business_km = business
    leg.private_km = leg.distance_km - business
    if business == 0:
        leg.classification = Classification.PRIVATE
    elif business == leg.distance_km:
        leg.classification = Classification.BUSINESS
    else:
        leg.classification = Classification.MIXED
    leg.status = status


def _spread(legs: list[Leg], business: Decimal, status: LegStatus) -> None:
    """Assign `business` km to the legs in order, the rest is private."""
    for leg in legs:
        part = min(business, leg.distance_km)
        _set(leg, part, status)
        business -= part


def split_segments(legs: list[Leg], locations: dict[str, Location]) -> list[list[Leg]]:
    def anchor(loc_id: str | None) -> bool:
        return loc_id in locations and locations[loc_id].type in BUSINESS_TYPES

    segments: list[list[Leg]] = []
    current: list[Leg] = []
    for leg in legs:
        current.append(leg)
        if anchor(leg.to_location):
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments


def _annotate_reason(legs: list[Leg], expected: int | None = None, crow: Decimal | None = None):
    for leg in legs:
        leg.expected_km = expected
        leg.crow_km = int(crow.to_integral_value(ROUND_FLOOR)) if crow is not None else None


def classify_segment(
    legs: list[Leg], distances: Distances, manual_days: frozenset[date] = frozenset()
) -> Segment:
    seg = _classify_segment(legs, distances)
    # rule 8: manual trips on a day of the gap mean the user took over: the leg is private
    gap_legs = [leg for leg in legs if FLAG_POLL_GAP in leg.flags]
    if any(d in manual_days for leg in gap_legs for d in gap_days(leg)):
        seg.rule, seg.reason, seg.question = 8, Reason.GAP_MANUAL, None
        _spread(legs, Decimal(0), LegStatus.AUTO)
    return seg


def _classify_segment(legs: list[Leg], distances: Distances) -> Segment:
    start, end = legs[0].from_location, legs[-1].to_location
    seg = Segment(legs=legs, rule=0)
    _annotate_reason(legs)

    if not distances.business_pair(start, end):
        # rule 5: unknown or private place, or back where it started: private, no question
        seg.rule = 5
        same = len(legs) == 1 and start is not None and start == end
        seg.reason = Reason.SAME_PLACE if same else Reason.NO_BUSINESS_PLACE
        _spread(legs, Decimal(0), LegStatus.AUTO)
        return seg

    expected = distances.expected(start, end, before=_when(legs[0]))
    crow = distances.crow_km(start, end)
    if len(legs) == 1:
        leg = legs[0]
        if expected is not None and leg.distance_km <= expected + margin(expected):
            seg.rule, seg.reason = 1, Reason.FITS
            _set(leg, leg.distance_km)
        elif FLAG_POLL_GAP in leg.flags:
            # rule 8: what else the car did during the gap is unknown; no detour question
            seg.rule, seg.reason = 8, Reason.GAP
            floor = crow.to_integral_value(ROUND_FLOOR)
            _set(leg, Decimal(expected) if expected is not None else floor)
        elif expected is not None:
            seg.rule, seg.reason = 2, Reason.DETOUR
            _set(leg, Decimal(expected), LegStatus.NEEDS_CONFIRMATION)
            seg.question = Question.DETOUR_OR_PRIVATE_STOP
            seg.business_if_confirmed = leg.distance_km
        elif distances.plausible(start, end, leg.distance_km):
            seg.rule, seg.reason = 1, Reason.FIRST_TRIP
            _set(leg, leg.distance_km)
        else:
            seg.rule, seg.reason = 2, Reason.IMPLAUSIBLE
            leg.flags.append(FLAG_IMPLAUSIBLE)
            _set(leg, Decimal(0), LegStatus.NEEDS_CONFIRMATION)
            seg.question = Question.BUSINESS_OR_PRIVATE
            seg.business_if_confirmed = leg.distance_km
    else:
        # rule 3: stop(s) at a private or unknown place between two business places
        seg.rule = 3
        if expected is not None:
            seg.reason, business = Reason.STOP, Decimal(expected)
        else:
            seg.reason, business = Reason.STOP_CROW, crow.to_integral_value(ROUND_FLOOR)
        _spread(legs, business, LegStatus.AUTO)
    _annotate_reason(legs, expected, crow)

    # rule 6: weekend or holiday: private without a question; "Toch zakelijk" gives the
    # weekday outcome (its default, if that had a question)
    if not is_workday(legs[0].date):
        seg.rule, seg.reason = 6, Reason.WEEKEND
        seg.question = Question.WEEKEND
        seg.business_if_confirmed = seg.business_km
        for leg in legs:
            leg.flags.append(FLAG_WEEKEND_OR_HOLIDAY)
        _spread(legs, Decimal(0), LegStatus.AUTO)
    return seg


def _annotate(seg: Segment) -> None:
    """Store the segment context on its legs (they are persisted one by one)."""
    for leg in seg.legs:
        leg.segment_id = seg.legs[0].id
        leg.question = seg.question.value if seg.question else None
        leg.business_if_confirmed = seg.business_if_confirmed if seg.question else None
        leg.reason = seg.reason.value if seg.reason else None


def classify_legs(
    legs: list[Leg],
    locations: Iterable[Location],
    history: Iterable[Leg] = (),
    manual_days: Iterable[date] = (),
) -> list[Segment]:
    """Classify `legs` (in time order). `history`: earlier legs (e.g. from the store) to learn
    the expected distances from; the legs themselves are learned as they go. `manual_days`:
    days with manual trips (rule 8)."""
    manual_days = frozenset(manual_days)
    by_id = {loc.id: loc for loc in locations}
    distances = Distances(by_id)
    for leg in history:
        distances.learn(leg)
    segments = []
    for legs_ in split_segments(legs, by_id):
        seg = classify_segment(legs_, distances, manual_days)
        _annotate(seg)
        segments.append(seg)
        for leg in legs_:
            distances.learn(leg)
    return segments


def apply_answer(segment: Segment, answer: Answer) -> None:
    """Apply a confirmation answer. Business answers count `business_if_confirmed`; private
    answers keep the conservative default that classification already assigned."""
    if segment.question is not None and answer not in OPTIONS[segment.question]:
        raise ValueError(f"{answer} is not an option for {segment.question}")
    if answer in (Answer.DETOUR, Answer.BUSINESS, Answer.CONFIRM):
        _spread(segment.legs, segment.business_if_confirmed, LegStatus.CONFIRMED)
    elif answer is Answer.PRIVATE and segment.question is Question.WEEKEND:
        _spread(segment.legs, Decimal(0), LegStatus.CONFIRMED)
    else:
        for leg in segment.legs:
            leg.status = LegStatus.CONFIRMED


def apply_stored_answer(segment: Segment, answer: str) -> bool:
    """Re-apply a persisted answer after re-derivation. Answers that no longer fit the segment
    (the question changed or disappeared, e.g. after a stop was saved as a location) are
    ignored, so a stale answer can never increase the reimbursed km."""
    if segment.question is None:
        return False
    try:
        parsed = Answer(answer)
    except ValueError:
        return False
    if parsed not in OPTIONS[segment.question]:
        return False
    apply_answer(segment, parsed)
    return True


def time_out(segment: Segment) -> None:
    """No answer within CONFIRM_TIMEOUT_DAYS: the conservative default stands."""
    for leg in segment.legs:
        if leg.status is LegStatus.NEEDS_CONFIRMATION:
            leg.status = LegStatus.TIMED_OUT


# --- explanation --------------------------------------------------------------------------

_WEEKDAY = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")


def explain(leg: Leg) -> str:
    """One line (Dutch) on why the leg's segment got its outcome; "" for legs without one."""
    reason = _explain_reason(leg)
    if FLAG_POLL_GAP in leg.flags and leg.departed_after and leg.arrived_at:
        gap = f"Geen metingen tussen {moment(leg.departed_after)} en {moment(leg.arrived_at)}."
        return f"{gap} {reason}".strip()
    return reason


def moment(ts: datetime) -> str:
    """'vr 23-10 19:00' (Amsterdam)."""
    local = ts.astimezone(AMSTERDAM)
    return f"{_WEEKDAY[local.weekday()][:2]} {local:%d-%m %H:%M}"


def _explain_reason(leg: Leg) -> str:
    e, crow = leg.expected_km, leg.crow_km
    match leg.reason:
        case Reason.FITS:
            return f"Zakelijk: past bij de verwachte {e} km (t/m {int(e + margin(e))} km)."
        case Reason.FIRST_TRIP:
            return f"Zakelijk: eerste rit hierheen, aannemelijk (hemelsbreed {crow} km)."
        case Reason.DETOUR:
            return (
                f"Langer dan verwacht ({e} km, t/m {int(e + margin(e))} km): zakelijk telt {e} km "
                "tot je voor omweg kiest."
            )
        case Reason.IMPLAUSIBLE:
            return (
                f"Eerste rit hierheen, maar niet aannemelijk (hemelsbreed {crow} km): "
                "telt pas als je bevestigt."
            )
        case Reason.STOP:
            return f"Tussenstop: zakelijk telt de verwachte {e} km, de rest is privé."
        case Reason.STOP_CROW:
            return f"Tussenstop, nog geen eerdere rit: zakelijk telt hemelsbreed {crow} km."
        case Reason.NO_BUSINESS_PLACE:
            return "Privé: geen klant of zakelijke plek."
        case Reason.SAME_PLACE:
            return "Privé: begint en eindigt op dezelfde plek."
        case Reason.WEEKEND:
            day = "feestdag" if leg.date.weekday() < 5 else _WEEKDAY[leg.date.weekday()]
            if leg.status is LegStatus.CONFIRMED and leg.business_km:
                return f"{day.capitalize()}, toch zakelijk."
            km = int(leg.business_if_confirmed or 0)
            return f"Privé: {day}. Toch zakelijk? Dan telt {km} km."
        case Reason.GAP if e is not None:
            return f"Zakelijk telt de verwachte {e} km; wat de auto verder reed, is privé."
        case Reason.GAP:
            return f"Zakelijk telt hemelsbreed {crow} km; wat de auto verder reed, is privé."
        case Reason.GAP_MANUAL:
            return "Privé: je hebt voor deze dagen zelf ritten toegevoegd."
    return ""


# --- day-level checks (rule 7) -------------------------------------------------------------


def missed_business_days(legs: Iterable[Leg]) -> list[date]:
    """Rule 7: workdays with driven km but no business km at all (informational)."""
    driven: dict[date, Decimal] = {}
    business: dict[date, Decimal] = {}
    for leg in legs:
        driven[leg.date] = driven.get(leg.date, Decimal(0)) + leg.distance_km
        business[leg.date] = business.get(leg.date, Decimal(0)) + leg.business_km
    return sorted(d for d, km in driven.items() if km > 0 and business[d] == 0 and is_workday(d))
