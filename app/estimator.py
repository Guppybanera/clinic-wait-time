"""Wait-time estimator: a pure function with no database access.

Implements the spec's formula  W = S + (sum(r) + k*A) / D  as a small simulation:
D doctors each become free at some time; patients are taken in queue order and each
starts when a doctor is free (and, for a booked patient, not before they are expected).
When everyone ahead is already in the waiting room, this gives the same answer as the
formula, but it also handles gaps between appointments and booked patients who have
not arrived yet.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

RANGE_WIDTH = 15          # minutes; the requirement's maximum
LOW_OFFSET = 7            # low = W - 7, high = W + 8
MIN_REMAINING = 2         # a visit in progress is assumed to have at least 2 minutes left
MIN_COMPLETED_FOR_AVG = 3
EXPECTED_ARRIVAL_WINDOW = timedelta(minutes=30)


@dataclass(frozen=True)
class VisitInput:
    id: int
    state: str                       # booked / waiting / in_visit
    is_booked: bool                  # booked appointment (vs walk-in)
    appointment_at: datetime | None
    checked_in_at: datetime | None
    started_at: datetime | None


@dataclass(frozen=True)
class DayInput:
    now: datetime
    opening_at: datetime             # today's opening date+time
    opened_at: datetime | None       # when staff tapped "Clinic open"
    delay_until: datetime | None     # set by "Running late"
    doctors_expected: int
    doctors_in_session: int
    default_visit_min: int
    completed_visit_minutes: list[float]


@dataclass(frozen=True)
class Estimate:
    visit_id: int
    position: int | None             # place in line among checked-in patients (1 = next)
    wait_min: float                  # W, minutes from now until expected start
    low_min: int
    high_min: int
    expected_start: datetime         # now + W
    start_low: datetime              # clock range, rounded to 5 minutes
    start_high: datetime


def round5(x: float) -> int:
    return int(math.floor(x / 5 + 0.5) * 5)


def wait_range(w: float) -> tuple[int, int]:
    low = round5(w - LOW_OFFSET)
    high = round5(w + RANGE_WIDTH - LOW_OFFSET)
    if low < 0:
        low = 0
    return low, high


def average_visit(day: DayInput) -> float:
    done = day.completed_visit_minutes
    if len(done) >= MIN_COMPLETED_FOR_AVG:
        return sum(done) / len(done)
    return float(day.default_visit_min)


def doctors(day: DayInput) -> int:
    d = day.doctors_in_session if day.opened_at else day.doctors_expected
    return max(1, d)


def start_offset(day: DayInput) -> float:
    """S: minutes until doctors start seeing patients."""
    now = day.now
    s = 0.0
    if day.opened_at is None:
        if day.opening_at > now:
            s = (day.opening_at - now).total_seconds() / 60
        else:
            # Past opening and nobody tapped "Clinic open": assume the delay so far continues.
            s = (now - day.opening_at).total_seconds() / 60
    if day.delay_until and day.delay_until > now:
        s = max(s, (day.delay_until - now).total_seconds() / 60)
    return s


def queue_key(v: VisitInput) -> datetime:
    """Booked patients keep their appointment time; walk-ins go by check-in time and fill gaps."""
    if v.is_booked and v.appointment_at is not None:
        return v.appointment_at
    return v.checked_in_at or datetime.max


def estimate_all(day: DayInput, visits: list[VisitInput]) -> dict[int, Estimate]:
    now = day.now
    A = average_visit(day)
    D = doctors(day)
    S = start_offset(day)
    earliest = now + timedelta(minutes=S)

    # When each doctor is next free.
    in_visit = [v for v in visits if v.state == "in_visit"]
    free: list[datetime] = []
    for v in in_visit:
        elapsed = (now - (v.started_at or now)).total_seconds() / 60
        remaining = max(A - elapsed, MIN_REMAINING)
        free.append(now + timedelta(minutes=remaining))
    free.sort()
    free = free[:D] if len(free) > D else free + [now] * (D - len(free))
    free = [max(t, earliest) for t in free]

    # A booked patient more than 30 minutes past their appointment who hasn't arrived is
    # probably late or a no-show: they don't hold up people who are already waiting, and
    # are estimated as if they arrived now, at the back of the line.
    overdue = [v for v in visits if v.state == "booked" and v.appointment_at
               and v.appointment_at < now - EXPECTED_ARRIVAL_WINDOW]
    overdue_ids = {v.id for v in overdue}
    queue = sorted((v for v in visits if v.state in ("waiting", "booked") and v.id not in overdue_ids),
                   key=lambda v: (queue_key(v), v.id))
    queue += sorted(overdue, key=lambda v: (v.appointment_at, v.id))

    results: dict[int, Estimate] = {}
    position = 0
    for v in queue:
        if v.state == "booked":
            available = max(v.appointment_at or now, now)
        else:
            available = now
            position += 1
        free.sort()
        start = max(free[0], available)
        free[0] = start + timedelta(minutes=A)

        w = (start - now).total_seconds() / 60
        low, high = wait_range(w)
        results[v.id] = Estimate(
            visit_id=v.id,
            position=position if v.state == "waiting" else None,
            wait_min=w,
            low_min=low,
            high_min=high,
            expected_start=start,
            start_low=_round_clock(now + timedelta(minutes=low)),
            start_high=_round_clock(now + timedelta(minutes=high)),
        )
    return results


def walk_in_estimate(day: DayInput, visits: list[VisitInput]) -> Estimate:
    """What a walk-in arriving now could expect (for WAIT texts from numbers with no visit)."""
    probe = VisitInput(id=-1, state="waiting", is_booked=False, appointment_at=None,
                       checked_in_at=day.now, started_at=None)
    return estimate_all(day, visits + [probe])[-1]


def _round_clock(t: datetime) -> datetime:
    minutes = t.hour * 60 + t.minute + t.second / 60
    r = round5(minutes)
    return t.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=r)
