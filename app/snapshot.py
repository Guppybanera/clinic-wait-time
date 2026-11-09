"""Reads today's state from the database and runs the estimator over it.

Shared by the queue service, the notifier, the background job and the pages.
Only reads (apart from creating the config/day rows the first time they're needed).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock
from .estimator import DayInput, Estimate, VisitInput, average_visit, estimate_all, walk_in_estimate
from .models import (ACTIVE_STATES, CANCELLED, DONE, IN_VISIT, TYPE_BOOKED, WAITING, ClinicConfig,
                     ClinicDay, Visit)


def get_config(db: Session) -> ClinicConfig:
    cfg = db.get(ClinicConfig, 1)
    if cfg is None:
        cfg = ClinicConfig(id=1)
        db.add(cfg)
        db.flush()
    return cfg


def get_day(db: Session, d: date | None = None) -> ClinicDay:
    d = d or clock.now().date()
    day = db.get(ClinicDay, d)
    if day is None:
        cfg = get_config(db)
        day = ClinicDay(day=d, opening_time=cfg.default_opening_time,
                        closing_time=cfg.default_closing_time,
                        doctors_expected=cfg.default_doctors, doctors_in_session=cfg.default_doctors)
        db.add(day)
        db.flush()
    return day


def opening_at(day: ClinicDay) -> datetime:
    return datetime.combine(day.day, day.opening_time)


def active_visits(db: Session, d: date) -> list[Visit]:
    return list(db.scalars(select(Visit).where(Visit.visit_date == d, Visit.state.in_(ACTIVE_STATES))))


def completed_minutes(db: Session, d: date) -> list[float]:
    rows = db.scalars(select(Visit).where(Visit.visit_date == d, Visit.state == DONE,
                                          Visit.started_at.is_not(None), Visit.ended_at.is_not(None)))
    return [(v.ended_at - v.started_at).total_seconds() / 60 for v in rows]


def day_input(db: Session, d: date | None = None) -> DayInput:
    day = get_day(db, d)
    cfg = get_config(db)
    return DayInput(
        now=clock.now(),
        opening_at=opening_at(day),
        opened_at=day.opened_at,
        delay_until=day.delay_until,
        doctors_expected=day.doctors_expected,
        doctors_in_session=day.doctors_in_session,
        default_visit_min=cfg.default_visit_min,
        completed_visit_minutes=completed_minutes(db, day.day),
    )


def to_input(v: Visit) -> VisitInput:
    return VisitInput(id=v.id, state=v.state, is_booked=v.appointment_at is not None,
                      appointment_at=v.appointment_at, checked_in_at=v.checked_in_at,
                      started_at=v.started_at)


def estimates(db: Session, d: date | None = None) -> dict[int, Estimate]:
    """Estimates for every booked and waiting visit today. Only today's queue is estimated."""
    today = clock.now().date()
    d = d or today
    if d != today:
        return {}
    return estimate_all(day_input(db, d), [to_input(v) for v in active_visits(db, d)])


def walk_in_now(db: Session) -> Estimate:
    today = clock.now().date()
    return walk_in_estimate(day_input(db, today), [to_input(v) for v in active_visits(db, today)])


def day_totals(db: Session, d: date | None = None) -> dict:
    """Headline numbers for the console. Averages are actual times from today's visits
    (None until there is something to average), not the estimator's blended figure."""
    d = d or clock.now().date()
    visits = list(db.scalars(select(Visit).where(Visit.visit_date == d)))
    visit_mins = completed_minutes(db, d)
    waits = [v.actual_wait_min for v in visits if v.actual_wait_min is not None]
    return {
        "booked": sum(v.type == TYPE_BOOKED and v.state != CANCELLED for v in visits),
        "in_clinic": sum(v.state in (WAITING, IN_VISIT) for v in visits),
        "completed": sum(v.state == DONE for v in visits),
        "avg_visit": sum(visit_mins) / len(visit_mins) if visit_mins else None,
        "avg_wait": sum(waits) / len(waits) if waits else None,
    }


def today_average(db: Session) -> float:
    return average_visit(day_input(db))


def is_stale(db: Session, minutes: int = 20) -> bool:
    """No staff action for 20 minutes while patients are waiting."""
    day = get_day(db)
    waiting = any(v.state == "waiting" for v in active_visits(db, day.day))
    if not waiting:
        return False
    last = day.last_staff_action_at
    return last is None or clock.now() - last > timedelta(minutes=minutes)
