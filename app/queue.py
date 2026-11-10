"""Queue service: the only code that changes visits and clinic days.

State machine:  booked -> waiting -> in_visit -> done,  plus no_show and cancelled.
Walk-ins start at waiting, on the walk-in list (queued_at is None). A manager moves them into
the appointment queue (move_to_queue); only then can their visit start.

Every change writes an audit entry, stamps `clinic_days.last_staff_action_at`,
recomputes today's estimates and lets the notifier send any texts that are due.
Each public function commits, then tries to deliver queued texts.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock, notifier, snapshot, sms
from .crypto import encrypt, last4, normalize_phone, phone_hash
from .models import (
    BOOKED, CANCELLED, DONE, IN_VISIT, NO_SHOW, TYPE_BOOKED, TYPE_WALK_IN, WAITING,
    AuditLog, Visit,
)

UNDO_WINDOW = timedelta(minutes=2)

TRANSITIONS = {
    "check_in": ({BOOKED}, WAITING),
    "start": ({WAITING}, IN_VISIT),
    "end": ({IN_VISIT}, DONE),
    "no_show": ({BOOKED, WAITING}, NO_SHOW),
    "cancel": ({BOOKED, WAITING}, CANCELLED),
}


class QueueError(Exception):
    """A request that can't be done in the visit's current state (shown to staff)."""


def new_token() -> str:
    return secrets.token_urlsafe(16)  # 22 characters, 128 bits


# --- helpers ---------------------------------------------------------------

def _audit(db: Session, staff_id: int | None, action: str, visit: Visit | None,
           from_state: str | None = None, to_state: str | None = None) -> None:
    db.add(AuditLog(staff_user_id=staff_id, visit_id=visit.id if visit else None, action=action,
                    from_state=from_state, to_state=to_state, at=clock.now()))


def _touch(db: Session) -> None:
    snapshot.get_day(db).last_staff_action_at = clock.now()


def _finish(db: Session, *, touch: bool = True) -> None:
    if touch:
        _touch(db)
    db.flush()
    notifier.after_change(db)
    db.commit()
    sms.flush(db)


def _visit(db: Session, visit_id: int) -> Visit:
    v = db.get(Visit, visit_id)
    if v is None:
        raise QueueError("Visit not found")
    return v


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise QueueError("Name is required")
    return name[:100]


def _clean_phone(phone: str) -> str:
    p = normalize_phone(phone or "")
    if len(p.lstrip("+")) < 7:
        raise QueueError("Enter a valid phone number")
    return p


def _set_contact(v: Visit, name: str | None, phone: str | None) -> None:
    if name is not None:
        v.patient_name_encrypted = encrypt(_clean_name(name))
    if phone is not None:
        p = _clean_phone(phone)
        v.phone_encrypted = encrypt(p)
        v.phone_hash = phone_hash(p)
        v.phone_last4 = last4(p)


# --- appointments ----------------------------------------------------------

def book(db: Session, staff_id: int | None, *, name: str, phone: str, appointment_at: datetime) -> Visit:
    if appointment_at.date() < clock.now().date():
        raise QueueError("Can't book a date in the past")
    v = Visit(visit_date=appointment_at.date(), appointment_at=appointment_at, type=TYPE_BOOKED,
              state=BOOKED, link_token=new_token(), created_at=clock.now())
    _set_contact(v, name, phone)
    db.add(v)
    db.flush()
    _audit(db, staff_id, "book", v, None, BOOKED)
    notifier.on_book(db, v)  # queued before any pre-arrival text the recompute may send
    _finish(db)
    return v


def edit_booking(db: Session, staff_id: int | None, visit_id: int, *, appointment_at: datetime | None = None,
                 name: str | None = None, phone: str | None = None) -> Visit:
    v = _visit(db, visit_id)
    if v.state not in (BOOKED, WAITING):
        raise QueueError("This visit can no longer be edited")
    _set_contact(v, name, phone)
    if appointment_at is not None and v.type == TYPE_BOOKED and appointment_at != v.appointment_at:
        if v.state != BOOKED:
            raise QueueError("Patient is already checked in")
        v.appointment_at = appointment_at
        v.visit_date = appointment_at.date()
        # A new time means a new pre-arrival text.
        v.pre_arrival_sent_at = None
        v.last_notified_start_at = None
        v.last_delay_text_at = None
    _audit(db, staff_id, "edit", v)
    _finish(db)
    return v


def appointments_for(db: Session, d: date) -> list[Visit]:
    return list(db.scalars(select(Visit).where(Visit.visit_date == d, Visit.type == TYPE_BOOKED)
                           .order_by(Visit.appointment_at, Visit.id)))


def slots_for(db: Session, d: date) -> list[datetime]:
    cfg = snapshot.get_config(db)
    day = snapshot.get_day(db, d) if d == clock.now().date() else None
    opening = day.opening_time if day else cfg.default_opening_time
    closing = day.closing_time if day else cfg.default_closing_time
    t = datetime.combine(d, opening)
    end = datetime.combine(d, closing)
    step = timedelta(minutes=max(5, cfg.slot_length_min))
    out = []
    while t < end:
        out.append(t)
        t += step
    return out


# --- state changes ---------------------------------------------------------

def _transition(db: Session, staff_id: int | None, visit_id: int, action: str) -> Visit:
    allowed, to_state = TRANSITIONS[action]
    v = _visit(db, visit_id)
    if v.state not in allowed:
        raise QueueError(f"Can't {action.replace('_', ' ')} a visit that is {v.state.replace('_', ' ')}")
    now = clock.now()
    if action in ("check_in", "start", "end") and v.visit_date != now.date():
        raise QueueError("This visit is not today")
    if action == "start" and snapshot.on_walk_in_list(v):
        raise QueueError("This walk-in is still on the walk-in list — a manager must add them to the queue first")
    from_state = v.state
    v.state = to_state
    if action == "check_in":
        v.checked_in_at = now
    elif action == "start":
        v.started_at = now
        day = snapshot.get_day(db)
        if day.opened_at is None:
            day.opened_at = now  # a doctor is seeing patients, so the clinic is open
        if v.checked_in_at:
            v.actual_wait_min = round((now - v.checked_in_at).total_seconds() / 60)
    elif action == "end":
        v.ended_at = now
    _audit(db, staff_id, action, v, from_state, to_state)
    return v


def _after_check_in(db: Session, v: Visit) -> None:
    db.flush()
    e = snapshot.estimates(db).get(v.id)
    if e is not None:
        v.first_estimate_min = round(e.wait_min)
        notifier.on_check_in(db, v, e)


def check_in(db: Session, staff_id: int | None, visit_id: int) -> Visit:
    v = _transition(db, staff_id, visit_id, "check_in")
    _after_check_in(db, v)
    _finish(db)
    return v


def walk_in(db: Session, staff_id: int | None, *, name: str, phone: str) -> Visit:
    now = clock.now()
    v = Visit(visit_date=now.date(), appointment_at=None, type=TYPE_WALK_IN, state=WAITING,
              link_token=new_token(), checked_in_at=now, created_at=now)
    _set_contact(v, name, phone)
    db.add(v)
    db.flush()
    _audit(db, staff_id, "walk_in", v, None, WAITING)
    _after_check_in(db, v)
    _finish(db)
    return v


def move_to_queue(db: Session, staff_id: int | None, visit_id: int) -> Visit:
    """Manager decision: move a walk-in from the walk-in list into the appointment queue.
    They join by the time of the move (so they go after booked patients already due and
    fill the gap before later appointments) and get a revised estimate by text."""
    v = _visit(db, visit_id)
    if not snapshot.on_walk_in_list(v):
        raise QueueError("Only a walk-in on the walk-in list can be added to the queue")
    if v.visit_date != clock.now().date():
        raise QueueError("This visit is not today")
    now = clock.now()
    v.queued_at = now
    _audit(db, staff_id, "queue", v, WAITING, WAITING)
    db.flush()
    e = snapshot.estimates(db).get(v.id)
    if e is not None:
        # Accuracy is judged on the estimate made when they joined the queue
        # (time already waited + the revised wait), not the provisional one.
        waited = (now - v.checked_in_at).total_seconds() / 60 if v.checked_in_at else 0
        v.first_estimate_min = round(waited + e.wait_min)
        notifier.on_queued(db, v, e)
    _finish(db)
    return v


def start(db: Session, staff_id: int | None, visit_id: int) -> Visit:
    v = _transition(db, staff_id, visit_id, "start")
    _finish(db)
    return v


def end(db: Session, staff_id: int | None, visit_id: int) -> Visit:
    v = _transition(db, staff_id, visit_id, "end")
    _finish(db)
    return v


def no_show(db: Session, staff_id: int | None, visit_id: int) -> Visit:
    v = _transition(db, staff_id, visit_id, "no_show")
    _finish(db)
    return v


def cancel(db: Session, staff_id: int | None, visit_id: int) -> Visit:
    """Cancel a booking. No text is sent."""
    v = _transition(db, staff_id, visit_id, "cancel")
    _finish(db)
    return v


def undoable(db: Session, visit_id: int) -> AuditLog | None:
    """The last state change on this visit, if it can still be undone."""
    entry = db.scalars(select(AuditLog).where(AuditLog.visit_id == visit_id, AuditLog.to_state.is_not(None))
                       .order_by(AuditLog.id.desc()).limit(1)).first()
    if entry is None or entry.undone or clock.now() - entry.at > UNDO_WINDOW:
        return None
    v = db.get(Visit, visit_id)
    if v is None or v.state != entry.to_state:
        return None
    return entry


def undo(db: Session, staff_id: int | None, visit_id: int) -> Visit:
    entry = undoable(db, visit_id)
    if entry is None:
        raise QueueError("Nothing to undo (only the last action, within 2 minutes)")
    v = _visit(db, visit_id)
    current = v.state
    if entry.from_state is None:
        # Undoing the creation of a walk-in or booking.
        v.state = CANCELLED
    else:
        v.state = entry.from_state
    if entry.action == "check_in":
        v.checked_in_at = None
        v.first_estimate_min = None
    elif entry.action == "start":
        v.started_at = None
        v.actual_wait_min = None
    elif entry.action == "end":
        v.ended_at = None
    elif entry.action == "queue":
        v.queued_at = None  # back on the walk-in list
    entry.undone = True
    _audit(db, staff_id, f"undo_{entry.action}", v, current, None)
    _finish(db)
    return v


# --- clinic day ------------------------------------------------------------

def open_clinic(db: Session, staff_id: int | None) -> None:
    day = snapshot.get_day(db)
    now = clock.now()
    day.opened_at = now
    if day.delay_until and day.delay_until <= now:
        day.delay_until = None
    _audit(db, staff_id, "clinic_open", None)
    _finish(db)


def running_late(db: Session, staff_id: int | None, *, opening_at: time | None = None,
                 delay_min: int | None = None) -> None:
    """Either a new opening time ("opening at 10:30") or a delay ("doctor delayed 30 min")."""
    day = snapshot.get_day(db)
    now = clock.now()
    if opening_at is not None:
        until = datetime.combine(day.day, opening_at)
    elif delay_min is not None and delay_min > 0:
        until = now + timedelta(minutes=delay_min)
    else:
        raise QueueError("Enter a new opening time or a delay in minutes")
    if until <= now:
        raise QueueError("That time has already passed")
    day.delay_until = until
    _audit(db, staff_id, "running_late", None)
    _finish(db)


def clear_delay(db: Session, staff_id: int | None) -> None:
    snapshot.get_day(db).delay_until = None
    _audit(db, staff_id, "clear_delay", None)
    _finish(db)


def set_doctors(db: Session, staff_id: int | None, n: int) -> None:
    if not 1 <= n <= 9:
        raise QueueError("Doctors in session must be between 1 and 9")
    day = snapshot.get_day(db)
    day.doctors_in_session = n
    if day.opened_at is None:
        day.doctors_expected = n
    _audit(db, staff_id, "doctors", None)
    _finish(db)


def recompute(db: Session) -> None:
    """Time-based recompute from the background job (not a staff action)."""
    _finish(db, touch=False)
