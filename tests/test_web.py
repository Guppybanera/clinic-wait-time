"""Route tests, including the spec's traceability checks (page size, text size, refresh,
no names on patient surfaces, altered tokens)."""
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth, clock, queue, ratelimit
from app.db import get_db
from app.main import create_app
from app.models import Notification, Visit

D = datetime(2026, 11, 5)
NAMES = ["Anita Kumari", "Suresh Babu", "Meena Devi"]


def at(h, m=0):
    return D.replace(hour=h, minute=m)


@pytest.fixture
def client(db):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    ratelimit.patient_pages.reset()
    ratelimit.inbound_sms.reset()
    ratelimit.logins.reset()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def staff(db, client):
    auth.create_user(db, username="desk", name="Desk One", password="correct-horse-1")
    r = client.post("/staff/login", data={"username": "desk", "password": "correct-horse-1"},
                    follow_redirects=False)
    assert r.status_code == 303
    return client


@pytest.fixture
def busy_day(db):
    clock.set_now(at(9))
    queue.open_clinic(db, None)
    a = queue.walk_in(db, None, name=NAMES[0], phone="9840000001")
    b = queue.walk_in(db, None, name=NAMES[1], phone="9840000002")
    c = queue.book(db, None, name=NAMES[2], phone="9840000003", appointment_at=at(10))
    return a, b, c


def no_names(text):
    for n in NAMES:
        for part in n.split():
            assert not re.search(rf"\b{part}\b", text, re.IGNORECASE), part


# --- auth -------------------------------------------------------------------

def test_staff_pages_need_login(client):
    for url in ["/staff", "/staff/appointments", "/staff/settings", "/dev/outbox"]:
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/staff/login")


def test_wrong_password(db, client):
    auth.create_user(db, username="desk", name="Desk", password="correct-horse-1")
    r = client.post("/staff/login", data={"username": "desk", "password": "nope"})
    assert r.status_code == 401


def test_idle_lock(staff):
    assert staff.get("/staff").status_code == 200
    clock.set_now(clock.now() + timedelta(minutes=4))
    assert staff.get("/staff?auto=1").status_code == 200       # auto-refresh doesn't count
    clock.set_now(clock.now() + timedelta(minutes=2))
    r = staff.get("/staff", follow_redirects=False)
    assert r.status_code == 303 and "locked=1" in r.headers["location"]


def test_settings_manager_only(db, staff):
    assert staff.get("/staff/settings").status_code == 403


def test_open_redirect_blocked(db, client):
    auth.create_user(db, username="desk", name="Desk", password="correct-horse-1")
    r = client.post("/staff/login", data={"username": "desk", "password": "correct-horse-1",
                                          "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/staff"


# --- staff flows ------------------------------------------------------------

def test_console_shows_names_to_staff(staff, busy_day):
    r = staff.get("/staff")
    assert r.status_code == 200
    assert NAMES[0] in r.text and "…0001" in r.text
    assert "Start visit" in r.text


def test_walk_in_and_actions_via_forms(db, staff):
    clock.set_now(at(9))
    staff.post("/staff/clinic/open")
    r = staff.post("/staff/walk-in", data={"name": "Test Patient", "phone": "98400 11111"})
    assert r.status_code == 200
    v = db.scalar(select(Visit))
    assert v.state == "waiting"
    staff.post(f"/staff/visits/{v.id}/start")
    assert v.state == "in_visit"
    staff.post(f"/staff/visits/{v.id}/undo")
    assert v.state == "waiting"
    r = staff.post(f"/staff/visits/{v.id}/end")
    assert "Can&#39;t end" in r.text or "Can't end" in r.text


def test_book_from_appointments_screen(db, staff):
    clock.set_now(at(8))
    r = staff.get("/staff/appointments")
    assert "Book 09:00" in r.text
    staff.post("/staff/appointments", data={"d": "2026-11-06", "t": "09:15", "name": "Ravi Kumar",
                                            "phone": "9840012345"})
    r = staff.get("/staff/appointments?date=2026-11-06")
    assert "Ravi K." in r.text and "…2345" in r.text
    v = db.scalar(select(Visit))
    staff.post(f"/staff/appointments/{v.id}/edit", data={"d": "2026-11-06", "t": "11:00"})
    assert v.appointment_at == datetime(2026, 11, 6, 11, 0)
    sheet = staff.get("/staff/appointments/print?date=2026-11-06")
    assert "Ravi Kumar" in sheet.text


def test_running_late_form(db, staff, busy_day):
    staff.post("/staff/clinic/late", data={"opening": "", "delay": "45"})
    assert len(db.scalars(select(Notification).where(Notification.kind == "delay")).all()) >= 2


def test_manager_settings(db, client):
    auth.create_user(db, username="boss", name="Boss", password="correct-horse-1", role="manager")
    client.post("/staff/login", data={"username": "boss", "password": "correct-horse-1"})
    assert client.get("/staff/settings").status_code == 200
    form = {"clinic_name": "Anna Clinic", "clinic_phone": "044 1234", "default_visit_min": "12",
            "slot_length_min": "10", "default_opening_time": "08:30", "default_closing_time": "13:00",
            "default_doctors": "2", "pre_arrival_lead_min": "60", "alert_threshold_min": "15",
            "quiet_start": "20:00", "quiet_end": "07:00", "name_retention_days": "30",
            "timing_retention_days": "90", "idle_lock_min": "5"}
    client.post("/staff/settings", data=form)
    from app import snapshot
    cfg = snapshot.get_config(db)
    assert cfg.clinic_name == "Anna Clinic" and cfg.slot_length_min == 10
    client.post("/staff/settings/users", data={"username": "desk2", "name": "D2",
                                               "password": "longenough", "role": "front_desk"})
    assert "desk2" in client.get("/staff/settings").text


# --- patient page: traceability checks --------------------------------------

def test_patient_page_content_and_size(client, busy_day):
    a, b, c = busy_day
    r = client.get(f"/w/{b.link_token}")
    assert r.status_code == 200
    assert "2nd in line" in r.text and "about 10–25 min" in r.text and "Estimate" in r.text
    assert len(r.content) < 50_000                       # spec: < 200 KB, aim < 50 KB
    assert '<meta http-equiv="refresh" content="60">' in r.text
    assert "60000" in r.text                              # polls every 60 s
    sizes = [int(px) for px in re.findall(r"font(?:-size)?:\s*(?:\d+\s+)?(\d+)px", r.text)]
    assert sizes and min(sizes) >= 24                     # 18pt = 24px
    assert "<img" not in r.text and "@font-face" not in r.text and "src=" not in r.text
    no_names(r.text)


def test_booked_patient_page(client, busy_day):
    c = busy_day[2]
    r = client.get(f"/w/{c.link_token}")
    assert "Expected to be seen about 10:00–10:15" in r.text
    no_names(r.text)


def test_patient_api(client, busy_day):
    a = busy_day[0]
    r = client.get(f"/api/w/{a.link_token}")
    assert r.json()["headline"] == "1st in line"
    no_names(r.text)


def test_end_states(db, client, busy_day):
    a = busy_day[0]
    queue.start(db, None, a.id)
    assert "It&#39;s your turn" in client.get(f"/w/{a.link_token}").text
    queue.end(db, None, a.id)
    assert "Your visit is complete" in client.get(f"/w/{a.link_token}").text
    clock.set_now(at(9) + timedelta(days=1))
    assert "This link has expired" in client.get(f"/w/{a.link_token}").text


def test_altered_tokens_not_found(client, busy_day):
    tok = busy_day[0].link_token
    for bad in [tok[:-1] + ("A" if tok[-1] != "A" else "B"), tok[:-1], tok + "x", "x" * 22, "%2e%2e"]:
        r = client.get(f"/w/{bad}")
        assert r.status_code == 404 and "Link not found" in r.text
        no_names(r.text)


def test_stale_warning(db, client, busy_day):
    clock.set_now(at(9, 25))
    r = client.get(f"/w/{busy_day[0].link_token}")
    assert "Estimate may be out of date" in r.text


def test_patient_rate_limit(client, busy_day):
    tok = busy_day[0].link_token
    codes = [client.get(f"/api/w/{tok}").status_code for _ in range(61)]
    assert codes[-1] == 429 and codes[0] == 200


# --- SMS --------------------------------------------------------------------

def test_inbound_wait_webhook(db, client, busy_day):
    r = client.post("/api/sms/inbound", data={"From": "+919840000002", "Body": "WAIT"})
    assert r.status_code == 200 and "<Response/>" in r.text
    reply = db.scalars(select(Notification).where(Notification.kind == "reply")).one()
    assert "2nd in line" in reply.body
    client.post("/api/sms/inbound", data={"From": "+15550001111", "Body": "wait"})
    walk = db.scalars(select(Notification).where(Notification.kind == "reply")
                      .order_by(Notification.id.desc())).first()
    assert walk.body.startswith("Walk-in wait now")


def test_no_names_in_any_text(db, busy_day):
    clock.set_now(at(9, 1))
    queue.running_late(db, None, delay_min=60)
    bodies = [n.body for n in db.scalars(select(Notification))]
    assert len(bodies) >= 4
    for b in bodies:
        no_names(b)


def test_twilio_signature():
    from app.routes.patient import twilio_signature_ok
    # Expected value checked against twilio.request_validator.RequestValidator.
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    assert twilio_signature_ok(url, params, "0/KCTR6DLpKmkAf8muzZqo1nDgQ=", "12345")
    assert not twilio_signature_ok(url, params, "nope", "12345")


def test_dev_outbox(staff, busy_day):
    r = staff.get("/dev/outbox")
    assert r.status_code == 200 and "check_in" in r.text
    r = staff.post("/dev/sms", data={"phone": "9840000001", "body": "WAIT"})
    assert "Reply: You&#39;re 1st in line" in r.text or "Reply: You're 1st in line" in r.text


# --- manager: full contact details and ad-hoc texts --------------------------

@pytest.fixture
def manager(db, client):
    auth.create_user(db, username="boss", name="Boss", password="correct-horse-1", role="manager")
    client.post("/staff/login", data={"username": "boss", "password": "correct-horse-1"})
    return client


def test_manager_sees_full_name_and_phone(manager, busy_day):
    r = manager.get("/staff")
    assert "9840000001" in r.text and NAMES[0] in r.text
    r = manager.get("/staff/appointments")
    assert NAMES[2] in r.text and "9840000003" in r.text


def test_front_desk_sees_last4_only(staff, busy_day):
    r = staff.get("/staff")
    assert "9840000001" not in r.text and "…0001" in r.text
    r = staff.get("/staff/appointments")
    assert "Meena D." in r.text and NAMES[2] not in r.text
    assert staff.get("/staff/sms").status_code == 403
    assert staff.post("/staff/sms", data={"to": "all", "message": "hi"}).status_code == 403


def test_adhoc_text_to_one_patient(db, manager, busy_day):
    a = busy_day[0]
    r = manager.get(f"/staff/sms?visit={a.id}")
    assert r.status_code == 200 and "selected" in r.text
    r = manager.post("/staff/sms", data={"to": str(a.id), "message": "Please bring your  old reports.",
                                         "link": "1"})
    assert "Text sent to 1 patient" in r.text
    [n] = db.scalars(select(Notification).where(Notification.kind == "adhoc")).all()
    assert n.visit_id == a.id and n.status == "sent"
    assert n.body == f"Please bring your old reports. Live wait: http://localhost:8000/w/{a.link_token}"


def test_adhoc_text_to_everyone_today(db, manager, busy_day):
    r = manager.post("/staff/sms", data={"to": "all", "message": "Doctor delayed by 30 minutes."})
    assert "Text sent to 3 patients" in r.text
    bodies = [n.body for n in db.scalars(select(Notification).where(Notification.kind == "adhoc"))]
    assert bodies == ["Doctor delayed by 30 minutes."] * 3     # no link box ticked


def test_adhoc_text_refuses_names_and_empty(db, manager, busy_day):
    a = busy_day[0]
    r = manager.post("/staff/sms", data={"to": str(a.id), "message": "Hello Anita, please come in"})
    assert "must not include a patient" in r.text
    r = manager.post("/staff/sms", data={"to": "all", "message": "kumari ward is closed"})
    assert "must not include a patient" in r.text
    r = manager.post("/staff/sms", data={"to": str(a.id), "message": "   "})
    assert "Type a message" in r.text
    assert db.scalars(select(Notification).where(Notification.kind == "adhoc")).all() == []


def test_patient_page_has_refresh(client, busy_day):
    tok = busy_day[0].link_token
    r = client.get(f"/w/{tok}")
    assert f'href="/w/{tok}">Refresh</a>' in r.text
