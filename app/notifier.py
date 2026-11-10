"""Decides who gets a text and what it says.

Rules (see docs/SPEC.md, Notifier):
- Every text carries the numbers, never a name. The link is only for live updates.
- Pre-arrival: `pre_arrival_lead_min` before a booked appointment; deferred during quiet hours.
- Check-in: always sent (the patient is at the clinic).
- Delay: when the expected start moves more than `alert_threshold_min` later than the
  last time the patient was told; at most one per 15 minutes; deferred during quiet hours.
- WAIT replies: matched by the sender's phone hash; unknown numbers get the walk-in wait.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from sqlalchemy.orm import Session

from . import clock, snapshot, sms
from .config import settings
from .crypto import decrypt, phone_hash
from .estimator import Estimate, round5
from .models import BOOKED, IN_VISIT, WAITING, AuditLog, ClinicConfig, Visit

DELAY_TEXT_GAP = timedelta(minutes=15)


# --- wording ---------------------------------------------------------------

def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def clock_str(t: datetime) -> str:
    return t.strftime("%I:%M").lstrip("0")


def clock_range(e: Estimate) -> str:
    return f"{clock_str(e.start_low)}–{clock_str(e.start_high)} {e.start_high.strftime('%p').lower()}"


def wait_str(e: Estimate) -> str:
    return f"about {e.low_min}–{e.high_min} min"


def link(v: Visit) -> str:
    return f"{settings.base_url}/w/{v.link_token}" if v.link_token else ""


def _call(cfg: ClinicConfig) -> str:
    return f" Questions: call {cfg.clinic_phone}." if cfg.clinic_phone else ""


def day_time_str(t: datetime) -> str:
    """'Tue 10 Nov, 10:30 am' — the booked time as a patient would write it down."""
    return f"{t.strftime('%a')} {t.day} {t.strftime('%b')}, {clock_str(t)} {t.strftime('%p').lower()}"


def booking_text(cfg: ClinicConfig, v: Visit) -> str:
    when = day_time_str(v.appointment_at)
    body = f"Your appointment at {cfg.clinic_name} is booked for {when}."
    if v.appointment_at - clock.now() > timedelta(minutes=cfg.pre_arrival_lead_min):
        body += (f" We'll text you about {cfg.pre_arrival_lead_min} min before with the "
                 f"expected start time.")
    if v.appointment_at.date() == clock.now().date():
        body += " Reply WAIT for an update."  # WAIT replies only match today's visits
    return body + _call(cfg)


ESTIMATE_NOTE = ("Times are estimates based on average wait and visit times; "
                 "actual time may vary.")


def ampm(t: datetime) -> str:
    return f"{clock_str(t)} {t.strftime('%p').lower()}"


def delay_minutes(v: Visit, e: Estimate) -> int:
    """How far the expected start is behind the appointment, to the nearest 5 min (0 = on time)."""
    if not v.appointment_at:
        return 0
    return max(0, round5((e.expected_start - v.appointment_at).total_seconds() / 60))


def estimate_lines(v: Visit, e: Estimate) -> str:
    """The three lines every estimate text carries."""
    if v.appointment_at:
        d = delay_minutes(v, e)
        second = f"Estimated delay: {d} min" if d else "Estimated delay: None, on time"
        scheduled = ampm(v.appointment_at)
    else:
        # A walk-in has no scheduled time to be late against: give the wait instead.
        second = f"Estimated wait: {wait_str(e)}"
        scheduled = f"Walk-in (checked in {ampm(v.checked_in_at)})" if v.checked_in_at else "Walk-in"
    return (f"Scheduled visit time: {scheduled}\n"
            f"{second}\n"
            f"Estimated time: {clock_range(e)}")


def _estimate_text(cfg: ClinicConfig, v: Visit, e: Estimate, opening: str, wait: bool = True) -> str:
    parts = [opening, estimate_lines(v, e)]
    tail = "Reply WAIT for an update." if wait else ""
    if link(v):
        tail = f"{tail} Live: {link(v)}".strip()
    if tail:
        parts.append(tail)
    parts.append(ESTIMATE_NOTE)
    return "\n".join(parts)


def pre_arrival_text(cfg: ClinicConfig, v: Visit, e: Estimate) -> str:
    return _estimate_text(cfg, v, e, f"Your visit at {cfg.clinic_name}:")


def _list_place(e: Estimate) -> str:
    return f"{ordinal(e.walk_in_place)} on the walk-in list"


def check_in_text(cfg: ClinicConfig, v: Visit, e: Estimate) -> str:
    if e.walk_in_place:
        return _estimate_text(cfg, v, e, f"You're checked in at {cfg.clinic_name} as a walk-in, "
                                         f"{_list_place(e)}. We'll text a revised time when "
                                         f"you're added to the queue.")
    return _estimate_text(cfg, v, e, f"You're checked in at {cfg.clinic_name}. "
                                     f"You're {ordinal(e.position or 1)} in line.")


def queued_text(cfg: ClinicConfig, v: Visit, e: Estimate) -> str:
    return _estimate_text(cfg, v, e, f"You've been added to the queue at {cfg.clinic_name}. "
                                     f"You're {ordinal(e.position or 1)} in line. Revised estimate:")


def delay_text(cfg: ClinicConfig, v: Visit, e: Estimate) -> str:
    return _estimate_text(cfg, v, e, f"{cfg.clinic_name} is running behind. New estimate:")


def status_reply(cfg: ClinicConfig, v: Visit | None, e: Estimate | None) -> str:
    if v is None:
        return f"Walk-in wait now at {cfg.clinic_name}: {wait_str(e)}. This is an estimate.{_call(cfg)}"
    if v.state == IN_VISIT:
        return "It's your turn — please come to the front desk."
    if v.state == BOOKED:
        return _estimate_text(cfg, v, e, f"Your visit at {cfg.clinic_name}:", wait=False)
    if e.walk_in_place:
        return _estimate_text(cfg, v, e, f"You're {_list_place(e)} at {cfg.clinic_name}.", wait=False)
    return _estimate_text(cfg, v, e, f"You're {ordinal(e.position or 1)} in line at {cfg.clinic_name}.",
                          wait=False)


# --- rules -----------------------------------------------------------------

def in_quiet_hours(cfg: ClinicConfig, t: datetime | None = None) -> bool:
    now: time = (t or clock.now()).time()
    start, end = cfg.quiet_start, cfg.quiet_end
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def _phone(v: Visit) -> str | None:
    return decrypt(v.phone_encrypted) if v.phone_encrypted else None


def _send(db: Session, v: Visit, kind: str, body: str) -> bool:
    to = _phone(v)
    if not to:
        return False
    sms.enqueue(db, to=to, body=body, kind=kind, visit_id=v.id)
    return True


def on_check_in(db: Session, v: Visit, e: Estimate) -> None:
    cfg = snapshot.get_config(db)
    if _send(db, v, "check_in", check_in_text(cfg, v, e)):
        v.last_notified_start_at = e.expected_start


def on_book(db: Session, v: Visit) -> None:
    """Confirm a new booking straight away. Sent even in quiet hours: the patient has just
    asked for the appointment (usually on the phone with staff)."""
    cfg = snapshot.get_config(db)
    _send(db, v, "booking", booking_text(cfg, v))


def on_queued(db: Session, v: Visit, e: Estimate) -> None:
    """Walk-in moved into the queue: tell them their revised time."""
    cfg = snapshot.get_config(db)
    if _send(db, v, "queued", queued_text(cfg, v, e)):
        v.last_notified_start_at = e.expected_start


def send_pre_arrival(db: Session, v: Visit, e: Estimate) -> bool:
    cfg = snapshot.get_config(db)
    if in_quiet_hours(cfg):
        return False
    if not _send(db, v, "pre_arrival", pre_arrival_text(cfg, v, e)):
        return False
    now = clock.now()
    v.pre_arrival_sent_at = now
    v.last_notified_start_at = e.expected_start
    return True


def due_pre_arrivals(db: Session, ests: dict[int, Estimate]) -> int:
    cfg = snapshot.get_config(db)
    now = clock.now()
    lead = timedelta(minutes=cfg.pre_arrival_lead_min)
    sent = 0
    for v in snapshot.active_visits(db, now.date()):
        if (v.state == BOOKED and v.pre_arrival_sent_at is None and v.appointment_at
                and now < v.appointment_at <= now + lead and v.id in ests):
            sent += send_pre_arrival(db, v, ests[v.id])
    return sent


def check_delays(db: Session, ests: dict[int, Estimate]) -> int:
    """Text anyone whose expected start has moved more than the threshold later."""
    cfg = snapshot.get_config(db)
    now = clock.now()
    threshold = timedelta(minutes=cfg.alert_threshold_min)
    quiet = in_quiet_hours(cfg)
    sent = 0
    for vid, e in ests.items():
        v = db.get(Visit, vid)
        if v is None or v.state not in (BOOKED, WAITING) or v.last_notified_start_at is None:
            continue
        if e.expected_start - v.last_notified_start_at <= threshold:
            continue
        if e.wait_min < cfg.alert_threshold_min:
            # About to be seen anyway (e.g. staff haven't tapped yet): a "running behind"
            # text would only be noise.
            continue
        if quiet or (v.last_delay_text_at and now - v.last_delay_text_at < DELAY_TEXT_GAP):
            continue  # try again on a later recompute
        if _send(db, v, "delay", delay_text(cfg, v, e)):
            v.last_notified_start_at = e.expected_start
            v.last_delay_text_at = now
            sent += 1
    return sent


def handle_inbound(db: Session, from_number: str, body: str) -> str:
    """Reply to an inbound text (normally "WAIT"). Returns the reply body, already queued."""
    cfg = snapshot.get_config(db)
    today = clock.now().date()
    h = phone_hash(from_number)
    matches = [v for v in snapshot.active_visits(db, today) if v.phone_hash == h]
    # Prefer someone already in the building, then the earliest appointment.
    order = {IN_VISIT: 0, WAITING: 1, BOOKED: 2}
    matches.sort(key=lambda v: (order[v.state], v.appointment_at or datetime.max, v.id))
    v = matches[0] if matches else None
    if v is None:
        reply = status_reply(cfg, None, snapshot.walk_in_now(db))
    else:
        e = snapshot.estimates(db).get(v.id)
        reply = status_reply(cfg, v, e)
        if e is not None:
            v.last_notified_start_at = e.expected_start
    n = sms.enqueue(db, to=from_number, body=reply, kind="reply", visit_id=v.id if v else None)
    return n.body


def after_change(db: Session) -> dict[int, Estimate]:
    """Recompute today's estimates and send whatever texts are now due."""
    ests = snapshot.estimates(db)
    due_pre_arrivals(db, ests)
    check_delays(db, ests)
    return ests


# --- ad-hoc texts (managers) -------------------------------------------------

ADHOC_MAX_CHARS = 300


class AdhocError(Exception):
    pass


def _mentions_name(body: str, v: Visit) -> bool:
    try:
        name = decrypt(v.patient_name_encrypted) if v.patient_name_encrypted else ""
    except Exception:  # noqa: BLE001
        name = ""
    return any(re.search(rf"\b{re.escape(part)}\b", body, re.IGNORECASE)
               for part in name.split() if len(part) >= 3)


def adhoc_body(v: Visit, message: str, include_link: bool) -> str:
    body = " ".join(message.split())
    if include_link and v.link_token:
        body = f"{body} Live wait: {link(v)}"
    return body


def send_adhoc(db: Session, staff_id: int | None, visits: list[Visit], message: str,
               include_link: bool = True) -> int:
    """Queue a manager-written text to each visit. Refuses texts that contain the patient's name
    (texts can be read by anyone holding the phone)."""
    message = " ".join((message or "").split())
    if not message:
        raise AdhocError("Type a message")
    if len(message) > ADHOC_MAX_CHARS:
        raise AdhocError(f"Keep the message under {ADHOC_MAX_CHARS} characters")
    targets = [v for v in visits if v.phone_encrypted]
    if not targets:
        raise AdhocError("No patient with a phone number to text")
    for v in targets:
        if _mentions_name(message, v):
            raise AdhocError("Texts must not include a patient's name — remove it and send again")
    now = clock.now()
    for v in targets:
        _send(db, v, "adhoc", adhoc_body(v, message, include_link))
        db.add(AuditLog(staff_user_id=staff_id, visit_id=v.id, action="adhoc_sms", at=now))
    return len(targets)
