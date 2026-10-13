"""Classification: legs -> business / private / needs-confirmation. See ONTWERP.md §7.2.

Rules 3 and 4 concern a return trip *with* a stop (client -> stop -> home), so classification
works on segments rather than single legs: a segment is a run of legs between two stops at
business-type locations (home, client, business), with zero or more private/unknown stops in
between. Leading/trailing legs that never reach such a location form a segment too.

Conservative default throughout (R6): an unanswered question never increases the reimbursed
amount. Test fixtures: the example days in ONTWERP.md §7.3.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from .model import Classification, Leg, LegStatus, Location, LocationType, Route

ROUTE_TOLERANCE_KM = 5
CONFIRM_TIMEOUT_DAYS = 7
PRIVATE_QUESTION_MIN_KM = 20

BUSINESS_TYPES = {LocationType.HOME, LocationType.CLIENT, LocationType.BUSINESS}
WORK_TYPES = {LocationType.CLIENT, LocationType.BUSINESS}

FLAG_WEEKEND_OR_HOLIDAY = "weekend_or_holiday"
FLAG_NO_ROUTE = "no_route"


class Question(StrEnum):
    DETOUR_OR_PRIVATE_STOP = "detour_or_private_stop"  # rule 2
    UNKNOWN_STOP = "unknown_stop"  # rule 4
    BUSINESS_OR_PRIVATE = "business_or_private"  # rule 5, and pairs without a known route
    WEEKEND = "weekend_or_holiday"  # rule 6: business trip on a weekend or holiday


class Answer(StrEnum):
    DETOUR = "detour"  # rule 2: all km business
    PRIVATE_STOP = "private_stop"  # rule 2: keep the default route
    BUSINESS = "business"
    PRIVATE = "private"
    CONFIRM = "confirm"  # rule 6: yes, this was business


OPTIONS = {
    Question.DETOUR_OR_PRIVATE_STOP: (Answer.DETOUR, Answer.PRIVATE_STOP),
    Question.UNKNOWN_STOP: (Answer.BUSINESS, Answer.PRIVATE),
    Question.BUSINESS_OR_PRIVATE: (Answer.BUSINESS, Answer.PRIVATE),
    Question.WEEKEND: (Answer.CONFIRM, Answer.PRIVATE),
}


@dataclass
class Segment:
    legs: list[Leg]
    rule: int
    question: Question | None = None
    # business km if the question is answered in the business direction
    business_if_confirmed: Decimal = Decimal(0)
    unknown_stops: list[tuple[float, float]] = field(default_factory=list)

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


# --- classification -----------------------------------------------------------------------


def _routes_between(routes: Iterable[Route], a: str, b: str) -> list[Route]:
    return [r for r in routes if r.connects(a, b)]


def _default_route(routes: list[Route]) -> Route | None:
    return next((r for r in routes if r.is_default), routes[0] if len(routes) == 1 else None)


def match_route(
    distance_km: Decimal, routes: list[Route], tolerance_km: int = ROUTE_TOLERANCE_KM
) -> Route | None:
    fits = [r for r in routes if abs(distance_km - r.distance_km) <= tolerance_km]
    return min(fits, key=lambda r: abs(distance_km - r.distance_km), default=None)


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


def classify_segment(
    legs: list[Leg],
    locations: dict[str, Location],
    routes: list[Route],
    tolerance_km: int = ROUTE_TOLERANCE_KM,
) -> Segment:
    start, end = legs[0].from_location, legs[-1].to_location
    start_loc, end_loc = locations.get(start or ""), locations.get(end or "")
    both_anchored = (
        start_loc is not None
        and end_loc is not None
        and start_loc.type in BUSINESS_TYPES
        and end_loc.type in BUSINESS_TYPES
        and start != end
    )
    stops = [leg.to_location for leg in legs[:-1]]
    unknown_stops = [leg.to_latlon for leg in legs[:-1] if leg.to_location not in locations]
    seg = Segment(legs=legs, rule=0, unknown_stops=[s for s in unknown_stops if s])
    total = seg.distance_km

    if not both_anchored:
        # rule 5: no known business pair (home -> somewhere -> home, unknown endpoints, ...)
        seg.rule = 5
        day = legs[0].date
        ask = is_workday(day) and total >= PRIVATE_QUESTION_MIN_KM
        _spread(legs, Decimal(0), LegStatus.NEEDS_CONFIRMATION if ask else LegStatus.AUTO)
        seg.question = Question.BUSINESS_OR_PRIVATE if ask else None
        seg.business_if_confirmed = total
        return seg

    pair_routes = _routes_between(routes, start, end)
    default = _default_route(pair_routes)

    if len(legs) == 1:
        leg = legs[0]
        matched = match_route(leg.distance_km, pair_routes, tolerance_km)
        if matched:
            seg.rule = 1
            leg.matched_route = matched.id
            _set(leg, leg.distance_km)
        elif default:
            seg.rule = 2
            leg.matched_route = default.id
            _set(leg, Decimal(default.distance_km), LegStatus.NEEDS_CONFIRMATION)
            seg.question = Question.DETOUR_OR_PRIVATE_STOP
            seg.business_if_confirmed = leg.distance_km
        else:
            # known business endpoints but no route on file: ask, count nothing until answered
            seg.rule = 2
            leg.flags.append(FLAG_NO_ROUTE)
            _set(leg, Decimal(0), LegStatus.NEEDS_CONFIRMATION)
            seg.question = Question.BUSINESS_OR_PRIVATE
            seg.business_if_confirmed = leg.distance_km
    else:
        # rules 3/4: intermediate stop(s) on the way between two business locations
        has_unknown = any(s not in locations for s in stops)
        seg.rule = 4 if has_unknown else 3
        business = Decimal(default.distance_km) if default else Decimal(0)
        if default is None:
            for leg in legs:
                leg.flags.append(FLAG_NO_ROUTE)
        status = LegStatus.NEEDS_CONFIRMATION if has_unknown else LegStatus.AUTO
        _spread(legs, business, status)
        if default:
            legs[0].matched_route = default.id
        if has_unknown:
            seg.question = Question.UNKNOWN_STOP
            seg.business_if_confirmed = total

    # rule 6: at a client / business location on a weekend or holiday
    work_end = any(locations[x].type in WORK_TYPES for x in (start, end) if x in locations)
    if work_end and not is_workday(legs[0].date):
        for leg in legs:
            leg.flags.append(FLAG_WEEKEND_OR_HOLIDAY)
            if leg.status is LegStatus.AUTO:
                leg.status = LegStatus.NEEDS_CONFIRMATION
        if seg.question is None:
            seg.question = Question.WEEKEND
            seg.business_if_confirmed = seg.business_km
    return seg


def _annotate(seg: Segment) -> None:
    """Store the segment context on its legs (they are persisted one by one)."""
    for leg in seg.legs:
        leg.segment_id = seg.legs[0].id
        leg.question = seg.question.value if seg.question else None
        leg.business_if_confirmed = seg.business_if_confirmed if seg.question else None


def classify_legs(
    legs: list[Leg],
    locations: Iterable[Location],
    routes: Iterable[Route],
    tolerance_km: int = ROUTE_TOLERANCE_KM,
) -> list[Segment]:
    by_id = {loc.id: loc for loc in locations}
    routes = list(routes)
    segments = [
        classify_segment(seg, by_id, routes, tolerance_km) for seg in split_segments(legs, by_id)
    ]
    for seg in segments:
        _annotate(seg)
    return segments


def apply_answer(segment: Segment, answer: Answer) -> None:
    """Apply a confirmation answer. Business answers count all km; private answers keep the
    conservative default that classification already assigned."""
    if segment.question is not None and answer not in OPTIONS[segment.question]:
        raise ValueError(f"{answer} is not an option for {segment.question}")
    if answer in (Answer.DETOUR, Answer.BUSINESS):
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


# --- day-level checks (rules 7 and 8) -------------------------------------------------------


def missed_business_days(legs: Iterable[Leg]) -> list[date]:
    """Rule 7: workdays with driven km but no business km at all (informational)."""
    driven: dict[date, Decimal] = {}
    business: dict[date, Decimal] = {}
    for leg in legs:
        driven[leg.date] = driven.get(leg.date, Decimal(0)) + leg.distance_km
        business[leg.date] = business.get(leg.date, Decimal(0)) + leg.business_km
    return sorted(d for d, km in driven.items() if km > 0 and business[d] == 0 and is_workday(d))


def day_mismatch(day_delta_km: int, legs_km: Decimal, threshold: float = 0.2) -> bool:
    """Rule 8: odometer delta of a day deviates > 20% from the sum of its legs."""
    if day_delta_km == 0:
        return legs_km != 0
    return abs(Decimal(day_delta_km) - legs_km) / Decimal(day_delta_km) > Decimal(str(threshold))
