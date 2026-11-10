import re
from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import select

from app import clock, jobs, notifier, queue, snapshot, sms
from app.crypto import decrypt
from app.models import AuditLog, ClinicDay, Notification, Visit

D = datetime(2026, 11, 5)


def at(h, m=0):
    return D.replace(hour=h, minute=m)


def texts(db, kind=None):
    q = select(Notification).order_by(Notification.id)
    if kind:
        q = q.where(Notification.kind == kind)
    return list(db.scalars(q))


def assert_no_names(db, *names):
    for n in texts(db):
        for name in names:
            for part in name.split():
                assert not re.search(rf"\b{part}\b", n.body, re.IGNORECASE), part


# --- state machine ----------------------------------------------------------

def test_walk_in_lifecycle(db, phone):
    clock.set_now(at(9, 5))
    queue.open_clinic(db, None)
    v = queue.walk_in(db, None, name="Ravi Kumar", phone="+91 98400 12345")
    assert v.state == "waiting" and v.checked_in_at == at(9, 5)
    assert len(v.link_token) == 22
    assert v.first_estimate_min == 0
    assert decrypt(v.patient_name_encrypted) == "Ravi Kumar"
    assert v.phone_last4 == "2345"

    clock.set_now(at(9, 20))
    queue.start(db, None, v.id)
    assert v.state == "in_visit" and v.actual_wait_min == 15
    clock.set_now(at(9, 32))
    queue.end(db, None, v.id)
    assert v.state == "done" and v.ended_at == at(9, 32)

    actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert actions == ["clinic_open", "walk_in", "start", "end"]
    assert snapshot.get_day(db).last_staff_action_at == at(9, 32)

    [t] = texts(db, "check_in")
    assert t.status == "sent"
    assert "1st in line" in t.body and "/w/" + v.link_token in t.body
    assert phone.sent[0][0] == "+919840012345"
    assert_no_names(db, "Ravi Kumar")


def test_invalid_transitions(db):
    v = queue.walk_in(db, None, name="A B", phone="9840012345")
    with pytest.raises(queue.QueueError):
        queue.end(db, None, v.id)
    with pytest.raises(queue.QueueError):
        queue.check_in(db, None, v.id)
    queue.start(db, None, v.id)
    with pytest.raises(queue.QueueError):
        queue.no_show(db, None, v.id)


def test_validation(db):
    with pytest.raises(queue.QueueError):
        queue.walk_in(db, None, name="  ", phone="9840012345")
    with pytest.raises(queue.QueueError):
        queue.walk_in(db, None, name="X", phone="12")
    with pytest.raises(queue.QueueError):
        queue.book(db, None, name="X", phone="9840012345", appointment_at=at(9) - timedelta(days=1))


def test_booking_and_check_in(db, phone):
    clock.set_now(at(7))  # before quiet hours end: no pre-arrival yet
    v = queue.book(db, None, name="Ravi K", phone="9840011111", appointment_at=at(10))
    assert v.state == "booked" and v.link_token and v.checked_in_at is None
    # Booking confirmation goes out at once, even in quiet hours; no pre-arrival yet.
    [t] = texts(db)
    assert t.kind == "booking"
    assert "booked for Thu 5 Nov, 10:00 am" in t.body and "Ravi" not in t.body
    assert "We'll text you about 90 min before" in t.body and "Reply WAIT" in t.body

    clock.set_now(at(9, 50))
    queue.open_clinic(db, None)
    queue.check_in(db, None, v.id)
    assert v.state == "waiting" and v.checked_in_at == at(9, 50)
    assert [t.kind for t in texts(db)] == ["booking", "pre_arrival", "check_in"]


def test_booking_text_for_another_day_and_soon(db, phone):
    clock.set_now(at(9))
    v = queue.book(db, None, name="Meena Devi", phone="9840011111",
                   appointment_at=at(16, 30) + timedelta(days=5))
    [t] = texts(db, "booking")
    assert "booked for Tue 10 Nov, 4:30 pm." in t.body and "Meena" not in t.body
    assert "Reply WAIT" not in t.body  # WAIT only answers for today's visits
    assert phone.sent[-1][1] == t.body
    # Within the pre-arrival lead: no "we'll text you before" promise; the pre-arrival
    # text follows the confirmation.
    queue.book(db, None, name="Ravi Kumar", phone="9840022222", appointment_at=at(10))
    kinds = [t.kind for t in texts(db) if t.visit_id != v.id]
    assert kinds == ["booking", "pre_arrival"]
    assert "We'll text you" not in texts(db, "booking")[-1].body


def test_cancel_sends_nothing(db, phone):
    clock.set_now(at(7))
    v = queue.book(db, None, name="Ravi K", phone="9840011111", appointment_at=at(15))
    queue.cancel(db, None, v.id)
    assert v.state == "cancelled"
    assert [t.kind for t in texts(db)] == ["booking"]  # only the confirmation


def test_edit_booking_resets_pre_arrival(db, phone):
    clock.set_now(at(9))
    v = queue.book(db, None, name="Ravi K", phone="9840011111", appointment_at=at(10))
    assert v.pre_arrival_sent_at is not None
    queue.edit_booking(db, None, v.id, appointment_at=at(14), phone="9840022222")
    assert v.pre_arrival_sent_at is None and v.appointment_at == at(14)
    assert v.phone_last4 == "2222"


# --- undo -------------------------------------------------------------------

def test_undo_start_within_two_minutes(db):
    v = queue.walk_in(db, None, name="A B", phone="9840012345")
    clock.set_now(at(9, 10))
    queue.start(db, None, v.id)
    clock.set_now(at(9, 11))
    queue.undo(db, None, v.id)
    assert v.state == "waiting" and v.started_at is None and v.actual_wait_min is None
    # Only once:
    with pytest.raises(queue.QueueError):
        queue.undo(db, None, v.id)


def test_undo_expires(db):
    v = queue.walk_in(db, None, name="A B", phone="9840012345")
    queue.no_show(db, None, v.id)
    clock.set_now(at(9, 3))
    with pytest.raises(queue.QueueError):
        queue.undo(db, None, v.id)


def test_undo_check_in_and_walk_in(db):
    b = queue.book(db, None, name="A B", phone="9840012345", appointment_at=at(9, 30))
    queue.check_in(db, None, b.id)
    queue.undo(db, None, b.id)
    assert b.state == "booked" and b.checked_in_at is None
    w = queue.walk_in(db, None, name="C D", phone="9840054321")
    queue.undo(db, None, w.id)
    assert w.state == "cancelled"
    entries = list(db.scalars(select(AuditLog).where(AuditLog.visit_id == b.id).order_by(AuditLog.id)))
    assert [e.action for e in entries] == ["book", "check_in", "undo_check_in"]


# --- clinic day / estimates -------------------------------------------------

def test_running_late_and_delay_texts(db, phone):
    clock.set_now(at(9))
    queue.open_clinic(db, None)
    a = queue.walk_in(db, None, name="Anita Kumari", phone="9840000001")
    b = queue.walk_in(db, None, name="Suresh Babu", phone="9840000002")
    c = queue.book(db, None, name="Meena Devi", phone="9840000003", appointment_at=at(10))
    assert texts(db, "pre_arrival")  # 60 min ahead: sent immediately

    # A 10-minute delay is absorbed by the gap before C's 10:00 appointment...
    clock.set_now(at(9, 1))
    queue.running_late(db, None, delay_min=10)
    assert {t.visit_id for t in texts(db, "delay")} == set()   # within 15-min threshold
    # ...an hour is not.
    queue.running_late(db, None, delay_min=60)
    delays = texts(db, "delay")
    assert {t.visit_id for t in delays} == {a.id, b.id, c.id}
    assert all("running behind" in t.body for t in delays)
    body_c = next(t.body for t in delays if t.visit_id == c.id)
    # C booked 10:00; the clinic now starts at 10:01 behind two walk-ins.
    assert "Scheduled visit time: 10:00 am\nEstimated delay: 30 min\nEstimated time: 10:25–10:40 am" in body_c
    assert body_c.endswith(notifier.ESTIMATE_NOTE)
    body_a = next(t.body for t in delays if t.visit_id == a.id)
    assert "Scheduled visit time: Walk-in (checked in 9:00 am)\nEstimated wait: about" in body_a

    # A further small delay doesn't text again; a big one within 15 min is held back.
    clock.set_now(at(9, 5))
    queue.running_late(db, None, delay_min=100)
    assert len(texts(db, "delay")) == 3
    clock.set_now(at(9, 17))
    jobs.tick(db)
    assert len(texts(db, "delay")) == 6
    assert_no_names(db, "Anita Kumari", "Suresh Babu", "Meena Devi")


def test_running_late_new_opening_time(db):
    clock.set_now(at(8, 30))
    queue.running_late(db, None, opening_at=time(10, 30))
    assert snapshot.get_day(db).delay_until == at(10, 30)
    v = queue.walk_in(db, None, name="A B", phone="9840000001")
    assert v.first_estimate_min == 120
    with pytest.raises(queue.QueueError):
        queue.running_late(db, None, opening_at=time(8, 0))


def test_first_start_marks_clinic_open(db):
    v = queue.walk_in(db, None, name="A B", phone="9840000001")
    clock.set_now(at(9, 5))
    queue.start(db, None, v.id)
    assert snapshot.get_day(db).opened_at == at(9, 5)


def test_doctors_and_open(db):
    queue.set_doctors(db, None, 2)
    day = db.get(ClinicDay, D.date())
    assert day.doctors_in_session == 2 and day.doctors_expected == 2
    with pytest.raises(queue.QueueError):
        queue.set_doctors(db, None, 0)


def test_actual_wait_and_first_estimate_recorded(db):
    queue.open_clinic(db, None)
    a = queue.walk_in(db, None, name="A B", phone="9840000001")
    b = queue.walk_in(db, None, name="C D", phone="9840000002")
    assert (a.first_estimate_min, b.first_estimate_min) == (0, 15)
    clock.set_now(at(9, 2))
    queue.start(db, None, a.id)
    clock.set_now(at(9, 19))
    queue.end(db, None, a.id)
    queue.start(db, None, b.id)
    assert b.actual_wait_min == 19
    n, pct = jobs.accuracy(db)
    assert n == 2 and pct == 100


# --- notifier ---------------------------------------------------------------

def test_pre_arrival_lead_and_quiet_hours(db, phone):
    clock.set_now(at(6))
    v = queue.book(db, None, name="A B", phone="9840000001", appointment_at=at(8))
    jobs.tick(db)
    assert texts(db, "pre_arrival") == []   # 06:30 would be due, but quiet until 07:00
    clock.set_now(at(6, 59))
    jobs.tick(db)
    assert texts(db, "pre_arrival") == []
    clock.set_now(at(7))
    jobs.tick(db)
    [t] = texts(db, "pre_arrival")
    assert "Reply WAIT" in t.body and v.pre_arrival_sent_at == at(7)
    # Booked for 8:00 but the clinic opens at 9:00, so the text says so.
    assert "Scheduled visit time: 8:00 am\nEstimated delay: 60 min\nEstimated time: 8:55–9:10 am" in t.body
    jobs.tick(db)
    assert len(texts(db, "pre_arrival")) == 1   # only once


def test_pre_arrival_not_before_lead(db):
    clock.set_now(at(8))
    queue.book(db, None, name="A B", phone="9840000001", appointment_at=at(10))
    assert texts(db, "pre_arrival") == []
    clock.set_now(at(8, 30))
    jobs.tick(db)
    assert len(texts(db, "pre_arrival")) == 1


def test_inbound_wait(db, phone):
    clock.set_now(at(9))
    queue.open_clinic(db, None)
    queue.walk_in(db, None, name="A B", phone="+91 98400 00001")
    reply = notifier.handle_inbound(db, "+919840000002", "WAIT")
    assert reply.startswith("Walk-in wait now")
    queue.walk_in(db, None, name="C D", phone="9840000002")
    reply = notifier.handle_inbound(db, "+919840000002", "wait")
    assert "2nd in line" in reply and "C D" not in reply
    db.commit()
    sms.flush(db)
    assert phone.sent[-1] == ("+919840000002", reply)


def test_inbound_booked_and_in_visit(db):
    clock.set_now(at(8))
    v = queue.book(db, None, name="A B", phone="9840000001", appointment_at=at(11))
    reply = notifier.handle_inbound(db, "9840000001", "WAIT")
    assert "Scheduled visit time: 11:00 am\nEstimated delay: None, on time\n" in reply
    assert "Estimated time: 11:00–11:15 am" in reply and notifier.ESTIMATE_NOTE in reply
    clock.set_now(at(10, 55))
    queue.open_clinic(db, None)
    queue.check_in(db, None, v.id)
    queue.start(db, None, v.id)
    assert "your turn" in notifier.handle_inbound(db, "9840000001", "WAIT")


def test_failed_texts_retry_then_give_up(db):
    from tests.conftest import FakeBackend
    down = FakeBackend(fail=True)
    sms.set_backend(down)
    queue.open_clinic(db, None)
    queue.walk_in(db, None, name="A B", phone="9840000001")
    [t] = texts(db)
    assert t.status == "queued" and t.attempts == 1
    assert "9840000001" not in (t.error or "")
    clock.set_now(at(9, 3))
    assert sms.texts_delayed(db)
    jobs.tick(db)
    assert t.attempts == 2 and t.status == "queued"
    clock.set_now(at(9, 31))
    jobs.tick(db)
    assert t.status == "failed"
    up = FakeBackend()
    sms.set_backend(up)
    jobs.tick(db)
    assert up.sent == []


def test_quiet_hours_wrap():
    cfg = type("C", (), {"quiet_start": time(20), "quiet_end": time(7)})()
    assert notifier.in_quiet_hours(cfg, at(21))
    assert notifier.in_quiet_hours(cfg, at(3))
    assert not notifier.in_quiet_hours(cfg, at(7))
    assert not notifier.in_quiet_hours(cfg, at(19, 59))


def test_ordinals():
    assert [notifier.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101, 111)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "101st", "111th"]


# --- nightly ----------------------------------------------------------------

def test_nightly_retention(db):
    clock.set_now(at(9))
    old = queue.walk_in(db, None, name="Old Name", phone="9840000001")
    mid = queue.walk_in(db, None, name="Mid Name", phone="9840000002")
    new = queue.walk_in(db, None, name="New Name", phone="9840000003")
    old.visit_date = (D - timedelta(days=91)).date()
    mid.visit_date = (D - timedelta(days=31)).date()
    new.visit_date = (D - timedelta(days=1)).date()
    for n in texts(db):
        if n.visit_id == old.id:
            n.created_at = D - timedelta(days=91)
    db.commit()

    out = jobs.nightly(db)
    assert out["visits_deleted"] == 1
    assert db.get(Visit, old.id) is None
    assert mid.patient_name_encrypted is None and mid.phone_encrypted is None and mid.phone_hash is None
    assert mid.link_token is None
    assert new.patient_name_encrypted is not None and new.link_token is None
    assert all(n.visit_id != old.id for n in texts(db))


# --- console totals ------------------------------------------------------------

def test_day_totals(db):
    clock.set_now(at(9))
    t = snapshot.day_totals(db)
    assert t == {"booked": 0, "in_clinic": 0, "completed": 0, "avg_visit": None, "avg_wait": None}
    queue.open_clinic(db, None)
    a = queue.walk_in(db, None, name="Ravi Kumar", phone="9840000001")        # 09:00
    b = queue.book(db, None, name="Meena Devi", phone="9840000002", appointment_at=at(9))
    c = queue.book(db, None, name="Arun Raj", phone="9840000003", appointment_at=at(11))
    d = queue.book(db, None, name="Lata Devi", phone="9840000004", appointment_at=at(12))
    queue.book(db, None, name="Ajay Raj", phone="9840000005", appointment_at=at(9) + timedelta(days=1))
    queue.cancel(db, None, d.id)
    queue.check_in(db, None, b.id)                                             # 09:00
    clock.set_now(at(9, 10)); queue.start(db, None, a.id)                      # waited 10
    clock.set_now(at(9, 30)); queue.end(db, None, a.id)                        # visit 20
    queue.start(db, None, b.id)                                                # waited 30
    t = snapshot.day_totals(db)
    # Booked today: b, c (d cancelled, the other is tomorrow). b is in a visit; a completed.
    assert t["booked"] == 2 and t["in_clinic"] == 1 and t["completed"] == 1
    assert t["avg_visit"] == 20 and t["avg_wait"] == 20
    clock.set_now(at(9, 40)); queue.end(db, None, b.id)                        # visit 10
    t = snapshot.day_totals(db)
    assert t["in_clinic"] == 0 and t["completed"] == 2 and t["avg_visit"] == 15
    assert c.state == "booked"
