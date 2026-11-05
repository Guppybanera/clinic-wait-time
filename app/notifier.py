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

from datetime import datetime, time, timedelta

from sqlalchemy.orm import Session

from . import clock, snapshot, sms
from .config import settings
from .crypto import decrypt, phone_hash
from .estimator import Estimate
from .models import BOOKED, IN_VISIT, WAITING, ClinicConfig, Visit

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


def pre_arrival_text(cfg: ClinicConfig, v: Visit, e: Estimate) -> str:
    return (f"Your visit at {cfg.clinic_name} is expected to start about {clock_range(e)}. "
            f"Reply WAIT for an update. Live: {link(v)}")


def check_in_text(cfg: ClinicConfig, v: Visit, e: Estimate) -> str:
    return (f"You're checked in at {cfg.clinic_name}. You're {ordinal(e.position or 1)} in line, "
            f"{wait_str(e)}. Live: {link(v)}")


def delay_text(cfg: ClinicConfig, v: Visit, e: Estimate) -> str:
    if v.state == BOOKED:
        new = f"Your visit is now expected to start about {clock_range(e)}."
    else:
        new = f"Your new estimate is {wait_str(e)}."
    return f"{cfg.clinic_name} is running behind. {new} {link(v)}".rstrip()


def status_reply(cfg: ClinicConfig, v: Visit | None, e: Estimate | None) -> str:
    if v is None:
        return f"Walk-in wait now at {cfg.clinic_name}: {wait_str(e)}. This is an estimate.{_call(cfg)}"
    if v.state == IN_VISIT:
        return "It's your turn — please come to the front desk."
    if v.state == BOOKED:
        return f"Your visit is expected to start about {clock_range(e)}. Live: {link(v)}"
    return f"You're {ordinal(e.position or 1)} in line, {wait_str(e)}. Live: {link(v)}"


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
    sms.enqueue(db, to=from_number, body=reply, kind="reply", visit_id=v.id if v else None)
    return reply


def after_change(db: Session) -> dict[int, Estimate]:
    """Recompute today's estimates and send whatever texts are now due."""
    ests = snapshot.estimates(db)
    due_pre_arrivals(db, ests)
    check_delays(db, ests)
    return ests
