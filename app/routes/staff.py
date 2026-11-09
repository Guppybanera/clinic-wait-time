"""Staff screens: login, today's queue console, appointments book, settings."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth, clock, jobs, notifier, queue, ratelimit, snapshot, sms
from ..db import get_db
from ..models import (
    BOOKED, CANCELLED, DONE, IN_VISIT, NO_SHOW, TYPE_BOOKED, WAITING, Notification, StaffUser, Visit,
)
from .common import back, flash, pop_flash, templates
from .patient import _client_ip

router = APIRouter()


def _safe_next(url: str | None, default: str = "/staff") -> str:
    if url and url.startswith("/staff") and "//" not in url and "\\" not in url:
        return url
    return default


# --- login -----------------------------------------------------------------

@router.get("/staff/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/staff", locked: int = 0):
    return templates.TemplateResponse(request, "login.html", {
        "next": _safe_next(next), "locked": locked, "error": None})


@router.post("/staff/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 next: str = Form("/staff"), db: Session = Depends(get_db)):
    if not ratelimit.logins.allow(_client_ip(request)):
        return templates.TemplateResponse(request, "login.html", {
            "next": _safe_next(next), "locked": 0, "error": "Too many attempts. Wait 5 minutes."},
            status_code=429)
    user = auth.authenticate(db, username, password)
    if user is None:
        return templates.TemplateResponse(request, "login.html", {
            "next": _safe_next(next), "locked": 0, "error": "Wrong username or password."}, status_code=401)
    auth.log_in(request, user)
    return back(_safe_next(next))


@router.post("/staff/logout")
def logout(request: Request):
    auth.log_out(request)
    return back("/staff/login")


# --- console ---------------------------------------------------------------

def _act(request: Request, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except queue.QueueError as e:
        flash(request, str(e), "error")


@router.get("/staff", response_class=HTMLResponse)
def console(request: Request, db: Session = Depends(get_db), user: StaffUser = Depends(auth.current_staff)):
    now = clock.now()
    day = snapshot.get_day(db)
    db.commit()
    ests = snapshot.estimates(db)
    visits = snapshot.active_visits(db, day.day)
    in_room = [v for v in visits if v.state in (WAITING, IN_VISIT)]
    in_room.sort(key=lambda v: (v.state != IN_VISIT, ests[v.id].position if v.id in ests else 0,
                                v.started_at or datetime.min))
    arriving = sorted((v for v in visits if v.state == BOOKED), key=lambda v: v.appointment_at)
    recent = [v for v in db.scalars(select(Visit).where(Visit.visit_date == day.day,
                                                        Visit.state.in_((DONE, NO_SHOW, CANCELLED))))
              if queue.undoable(db, v.id)]
    avg = snapshot.today_average(db)
    opening = snapshot.opening_at(day)
    n_acc, pct = jobs.accuracy(db)
    return templates.TemplateResponse(request, "console.html", {
        "user": user, "now": now, "day": day, "ests": ests, "in_room": in_room, "arriving": arriving,
        "recent": recent, "avg": avg, "totals": snapshot.day_totals(db, day.day),
        "not_open_late": day.opened_at is None and now > opening,
        "overrun": {v.id for v in in_room if v.state == IN_VISIT and v.started_at
                    and (now - v.started_at) > timedelta(minutes=2 * avg)},
        "undo": {v.id for v in visits if queue.undoable(db, v.id)},
        "walk_in_wait": snapshot.walk_in_now(db),
        "texts_delayed": sms.texts_delayed(db), "accuracy": (n_acc, pct),
        "flash": pop_flash(request),
    })


@router.post("/staff/walk-in")
def walk_in(request: Request, name: str = Form(""), phone: str = Form(""),
            db: Session = Depends(get_db), user: StaffUser = Depends(auth.current_staff)):
    _act(request, queue.walk_in, db, user.id, name=name, phone=phone)
    return back("/staff")


@router.post("/staff/visits/{visit_id}/{action}")
def visit_action(visit_id: int, action: str, request: Request, next: str = Form("/staff"),
                 db: Session = Depends(get_db), user: StaffUser = Depends(auth.current_staff)):
    fns = {"check-in": queue.check_in, "start": queue.start, "end": queue.end,
           "no-show": queue.no_show, "cancel": queue.cancel, "undo": queue.undo}
    fn = fns.get(action)
    if fn is None:
        flash(request, "Unknown action", "error")
    else:
        _act(request, fn, db, user.id, visit_id)
    return back(_safe_next(next))


@router.post("/staff/clinic/open")
def clinic_open(request: Request, db: Session = Depends(get_db), user: StaffUser = Depends(auth.current_staff)):
    _act(request, queue.open_clinic, db, user.id)
    return back("/staff")


@router.post("/staff/clinic/late")
def clinic_late(request: Request, opening: str = Form(""), delay: str = Form(""),
                db: Session = Depends(get_db), user: StaffUser = Depends(auth.current_staff)):
    try:
        opening_t = time.fromisoformat(opening) if opening.strip() else None
        delay_min = int(delay) if delay.strip() else None
    except ValueError:
        flash(request, "Enter a time like 10:30 or a number of minutes", "error")
        return back("/staff")
    _act(request, queue.running_late, db, user.id, opening_at=opening_t, delay_min=delay_min)
    return back("/staff")


@router.post("/staff/clinic/clear-delay")
def clinic_clear_delay(request: Request, db: Session = Depends(get_db),
                       user: StaffUser = Depends(auth.current_staff)):
    _act(request, queue.clear_delay, db, user.id)
    return back("/staff")


@router.post("/staff/clinic/doctors")
def clinic_doctors(request: Request, n: int = Form(...), db: Session = Depends(get_db),
                   user: StaffUser = Depends(auth.current_staff)):
    _act(request, queue.set_doctors, db, user.id, n)
    return back("/staff")


# --- appointments ----------------------------------------------------------

def _parse_date(s: str | None) -> date:
    try:
        return date.fromisoformat(s) if s else clock.now().date()
    except ValueError:
        return clock.now().date()


STATUS = {BOOKED: "Booked", WAITING: "Checked in", IN_VISIT: "In visit", DONE: "Done",
          NO_SHOW: "No-show", CANCELLED: "Cancelled"}


def status_label(v: Visit) -> str:
    if v.state == BOOKED and v.pre_arrival_sent_at:
        return "Texted"
    return STATUS.get(v.state, v.state)


templates.env.globals["status_label"] = status_label


def _day_rows(db: Session, d: date):
    """[(slot_time, [visits])] including any bookings at off-slot times."""
    slots = queue.slots_for(db, d)
    bookings = queue.appointments_for(db, d)
    by_time: dict[datetime, list[Visit]] = {t: [] for t in slots}
    for v in bookings:
        by_time.setdefault(v.appointment_at, []).append(v)
    return sorted(by_time.items())


@router.get("/staff/appointments", response_class=HTMLResponse)
def appointments(request: Request, date: str | None = None, db: Session = Depends(get_db),
                 user: StaffUser = Depends(auth.current_staff)):
    d = _parse_date(date)
    rows = _day_rows(db, d)
    db.commit()
    today = clock.now().date()
    return templates.TemplateResponse(request, "appointments.html", {
        "user": user, "d": d, "today": today, "rows": rows,
        "prev": d - timedelta(days=1), "next": d + timedelta(days=1),
        "slot_times": [t for t, _ in rows],
        "undo": {v.id for _, vs in rows for v in vs if queue.undoable(db, v.id)},
        "flash": pop_flash(request), "now": clock.now(),
    })


@router.get("/staff/appointments/print", response_class=HTMLResponse)
def print_sheet(request: Request, date: str | None = None, db: Session = Depends(get_db),
                user: StaffUser = Depends(auth.current_staff)):
    d = _parse_date(date)
    cfg = snapshot.get_config(db)
    bookings = [v for v in queue.appointments_for(db, d) if v.state != CANCELLED]
    db.commit()
    return templates.TemplateResponse(request, "day_sheet.html", {"d": d, "cfg": cfg, "bookings": bookings,
                                                               "user": user})


def _appt_dt(d: str, t: str) -> datetime:
    return datetime.combine(date.fromisoformat(d), time.fromisoformat(t))


@router.post("/staff/appointments")
def book(request: Request, d: str = Form(...), t: str = Form(...), name: str = Form(""),
         phone: str = Form(""), db: Session = Depends(get_db), user: StaffUser = Depends(auth.current_staff)):
    try:
        when = _appt_dt(d, t)
    except ValueError:
        flash(request, "Invalid date or time", "error")
        return back("/staff/appointments")
    _act(request, queue.book, db, user.id, name=name, phone=phone, appointment_at=when)
    return back(f"/staff/appointments?date={when.date().isoformat()}")


@router.post("/staff/appointments/{visit_id}/edit")
def edit(visit_id: int, request: Request, d: str = Form(...), t: str = Form(""), name: str = Form(""),
         phone: str = Form(""), db: Session = Depends(get_db), user: StaffUser = Depends(auth.current_staff)):
    v = db.get(Visit, visit_id)
    try:
        when = _appt_dt(d, t) if t and v is not None and v.type == TYPE_BOOKED else None
    except ValueError:
        flash(request, "Invalid date or time", "error")
        return back("/staff/appointments")
    _act(request, queue.edit_booking, db, user.id, visit_id, appointment_at=when,
         name=name or None, phone=phone or None)
    target = when.date() if when else (v.visit_date if v else clock.now().date())
    return back(f"/staff/appointments?date={target.isoformat()}")


# --- settings (manager) ----------------------------------------------------

CONFIG_FIELDS = {
    "clinic_name": str, "clinic_phone": str, "default_visit_min": int, "slot_length_min": int,
    "default_opening_time": time.fromisoformat, "default_closing_time": time.fromisoformat,
    "default_doctors": int, "pre_arrival_lead_min": int, "alert_threshold_min": int,
    "quiet_start": time.fromisoformat, "quiet_end": time.fromisoformat,
    "name_retention_days": int, "timing_retention_days": int, "idle_lock_min": int,
}


@router.get("/staff/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), user: StaffUser = Depends(auth.manager)):
    cfg = snapshot.get_config(db)
    day = snapshot.get_day(db)
    db.commit()
    users = list(db.scalars(select(StaffUser).order_by(StaffUser.name)))
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "cfg": cfg, "day": day, "users": users,
        "fields": {k: ("number" if c is int else "text" if c is str else "time") for k, c in CONFIG_FIELDS.items()},
        "flash": pop_flash(request)})


@router.post("/staff/settings")
async def settings_save(request: Request, db: Session = Depends(get_db), user: StaffUser = Depends(auth.manager)):
    form = await request.form()
    cfg = snapshot.get_config(db)
    try:
        values = {k: conv(str(form[k]).strip()) for k, conv in CONFIG_FIELDS.items() if k in form}
    except ValueError:
        flash(request, "Check the values: numbers and times like 09:00", "error")
        return back("/staff/settings")
    for k, val in values.items():
        if isinstance(val, int) and val < 0:
            flash(request, f"{k} can't be negative", "error")
            return back("/staff/settings")
        setattr(cfg, k, val)
    # Today's opening/closing hours follow the new defaults if the clinic hasn't opened yet.
    day = snapshot.get_day(db)
    if day.opened_at is None:
        day.opening_time, day.closing_time = cfg.default_opening_time, cfg.default_closing_time
    db.commit()
    flash(request, "Settings saved")
    return back("/staff/settings")


@router.post("/staff/settings/users")
def add_user(request: Request, username: str = Form(""), name: str = Form(""), password: str = Form(""),
             role: str = Form(auth.FRONT_DESK), db: Session = Depends(get_db),
             user: StaffUser = Depends(auth.manager)):
    try:
        auth.create_user(db, username=username, name=name, password=password, role=role)
        flash(request, f"Added {username}")
    except ValueError as e:
        flash(request, str(e), "error")
    return back("/staff/settings")


@router.post("/staff/settings/users/{uid}/toggle")
def toggle_user(uid: int, request: Request, db: Session = Depends(get_db), user: StaffUser = Depends(auth.manager)):
    u = db.get(StaffUser, uid)
    if u is None or u.id == user.id:
        flash(request, "You can't deactivate yourself", "error")
    else:
        u.active = not u.active
        db.commit()
    return back("/staff/settings")


# --- ad-hoc texts (manager) --------------------------------------------------

@router.get("/staff/sms", response_class=HTMLResponse)
def sms_page(request: Request, visit: int | None = None, db: Session = Depends(get_db),
             user: StaffUser = Depends(auth.manager)):
    today = clock.now().date()
    patients = sorted(snapshot.active_visits(db, today),
                      key=lambda v: (v.state != IN_VISIT, v.state != WAITING, v.appointment_at or v.checked_in_at
                                     or datetime.min))
    selected = db.get(Visit, visit) if visit else None
    if selected and selected not in patients:
        patients.insert(0, selected)   # e.g. a booking on another day
    recent = list(db.scalars(select(Notification).where(Notification.kind == "adhoc")
                             .order_by(Notification.id.desc()).limit(20)))
    db.commit()
    return templates.TemplateResponse(request, "sms.html", {
        "user": user, "patients": patients, "selected": selected, "recent": recent,
        "max_chars": notifier.ADHOC_MAX_CHARS, "flash": pop_flash(request)})


@router.post("/staff/sms")
def sms_send(request: Request, to: str = Form(""), message: str = Form(""), link: str = Form(""),
             db: Session = Depends(get_db), user: StaffUser = Depends(auth.manager)):
    today = clock.now().date()
    if to == "all":
        visits = [v for v in snapshot.active_visits(db, today) if v.state in (BOOKED, WAITING)]
    else:
        v = db.get(Visit, int(to)) if to.isdigit() else None
        visits = [v] if v is not None else []
    try:
        n = notifier.send_adhoc(db, user.id, visits, message, include_link=bool(link))
    except notifier.AdhocError as e:
        db.rollback()
        flash(request, str(e), "error")
        return back(f"/staff/sms?visit={to}" if to.isdigit() else "/staff/sms")
    db.commit()
    sms.flush(db)
    flash(request, f"Text sent to {n} patient{'s' if n != 1 else ''}")
    return back("/staff/sms")
