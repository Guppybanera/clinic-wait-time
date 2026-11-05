from datetime import datetime, timedelta

import pytest

from app.estimator import (
    DayInput,
    VisitInput,
    estimate_all,
    walk_in_estimate,
    wait_range,
)

DAY = datetime(2026, 11, 5)


def at(h, m=0):
    return DAY.replace(hour=h, minute=m)


def day(now, *, opened=True, opening=at(9), delay_until=None, doctors=1, expected=1,
        default_visit=15, completed=()):
    return DayInput(
        now=now,
        opening_at=opening,
        opened_at=opening if opened else None,
        delay_until=delay_until,
        doctors_expected=expected,
        doctors_in_session=doctors,
        default_visit_min=default_visit,
        completed_visit_minutes=list(completed),
    )


def walk_in(id, checked_in):
    return VisitInput(id=id, state="waiting", is_booked=False, appointment_at=None,
                      checked_in_at=checked_in, started_at=None)


def booked(id, appt, checked_in=None):
    return VisitInput(id=id, state="waiting" if checked_in else "booked", is_booked=True,
                      appointment_at=appt, checked_in_at=checked_in, started_at=None)


def in_visit(id, started):
    return VisitInput(id=id, state="in_visit", is_booked=False, appointment_at=None,
                      checked_in_at=started, started_at=started)


# --- range rule -------------------------------------------------------------

@pytest.mark.parametrize("w", [x / 4 for x in range(0, 4 * 240)])
def test_range_never_wider_than_15(w):
    low, high = wait_range(w)
    assert 0 <= low <= high
    assert high - low <= 15
    assert low % 5 == 0 and high % 5 == 0
    assert low <= w + 2.5 and high >= w - 2.5  # W sits inside (allowing for rounding)


def test_range_examples():
    assert wait_range(0) == (0, 10)
    assert wait_range(27.5) == (20, 35)
    assert wait_range(40) == (35, 50)


def test_every_estimate_range_at_most_15_across_many_queues():
    now = at(10, 3)
    for n in range(0, 12):
        for doctors in (1, 2, 3):
            visits = [walk_in(i, now - timedelta(minutes=30 - i)) for i in range(1, n + 1)]
            visits.append(in_visit(100, now - timedelta(minutes=4)))
            for est in estimate_all(day(now, doctors=doctors), visits).values():
                assert est.high_min - est.low_min <= 15
                assert est.start_high - est.start_low <= timedelta(minutes=15)


# --- formula case -----------------------------------------------------------

def formula(S, rs, k, A, D):
    return S + (sum(rs) + k * A) / D


def test_matches_formula_one_doctor():
    now = at(10)
    visits = [in_visit(1, now - timedelta(minutes=5))]           # r = 15 - 5 = 10
    visits += [walk_in(10 + i, now - timedelta(minutes=20 - i)) for i in range(3)]
    est = estimate_all(day(now), visits)
    for k, vid in enumerate([10, 11, 12]):
        assert est[vid].position == k + 1
        assert est[vid].wait_min == pytest.approx(formula(0, [10], k, 15, 1))


def test_matches_formula_two_doctors():
    now = at(11)
    visits = [in_visit(1, now - timedelta(minutes=5)), in_visit(2, now - timedelta(minutes=5))]
    visits += [walk_in(10 + i, now - timedelta(minutes=20 - i)) for i in range(3)]
    est = estimate_all(day(now, doctors=2), visits)
    # Third in line has k = 2 ahead: (10 + 10 + 2*15) / 2 = 25
    assert est[12].wait_min == pytest.approx(formula(0, [10, 10], 2, 15, 2))


def test_uses_todays_average_after_three_visits():
    now = at(12)
    visits = [walk_in(1, at(11, 50)), walk_in(2, at(11, 55))]
    est = estimate_all(day(now, completed=[10, 20, 30]), visits)   # A = 20
    assert est[2].wait_min == pytest.approx(20)
    est = estimate_all(day(now, completed=[30, 30]), visits)       # < 3 visits: default 15
    assert est[2].wait_min == pytest.approx(15)


def test_remaining_time_never_below_two_minutes():
    now = at(10)
    visits = [in_visit(1, now - timedelta(minutes=60)), walk_in(2, at(9, 30))]
    assert estimate_all(day(now), visits)[2].wait_min == pytest.approx(2)


# --- late opening (S) -------------------------------------------------------

def test_before_opening_counts_from_opening_time():
    now = at(8, 30)
    est = estimate_all(day(now, opened=False), [walk_in(1, at(8, 20)), walk_in(2, at(8, 25))])
    assert est[1].wait_min == pytest.approx(30)
    assert est[2].wait_min == pytest.approx(45)


def test_past_opening_not_tapped_slips():
    now = at(9, 20)
    est = estimate_all(day(now, opened=False), [walk_in(1, at(8, 50))])
    assert est[1].wait_min == pytest.approx(20)


def test_assumed_slip_is_capped():
    now = at(17)   # eight hours past opening, nobody tapped "Clinic open"
    est = estimate_all(day(now, opened=False), [walk_in(1, at(16, 50))])
    assert est[1].wait_min == pytest.approx(60)


def test_running_late_shifts_everyone():
    now = at(9, 10)
    visits = [walk_in(1, at(9)), walk_in(2, at(9, 5))]
    base = estimate_all(day(now), visits)
    late = estimate_all(day(now, delay_until=at(9, 40)), visits)
    assert late[1].wait_min - base[1].wait_min == pytest.approx(30)
    assert late[2].wait_min - base[2].wait_min == pytest.approx(30)


def test_delay_in_the_past_is_ignored():
    now = at(10)
    est = estimate_all(day(now, delay_until=at(9, 30)), [walk_in(1, at(9, 50))])
    assert est[1].wait_min == 0


# --- booked patients --------------------------------------------------------

def test_booked_patient_never_told_earlier_than_appointment():
    now = at(8, 30)
    for opened in (True, False):
        for appt_min in range(0, 240, 5):
            appt = at(9) + timedelta(minutes=appt_min)
            est = estimate_all(day(now, opened=opened), [booked(1, appt)])[1]
            assert est.expected_start >= appt
            assert est.start_low >= appt
            assert est.start_high - est.start_low <= timedelta(minutes=15)
            assert est.position is None


def test_booked_patient_pushed_later_by_queue():
    now = at(10)
    visits = [walk_in(i, at(9, 30) + timedelta(minutes=i)) for i in range(1, 5)]  # 60 min of work
    visits.append(booked(9, at(10, 15)))
    est = estimate_all(day(now), visits)[9]
    assert est.expected_start == at(11)
    assert est.start_low == at(10, 55) and est.start_high == at(11, 10)


def test_walk_ins_fill_gaps_before_appointments():
    now = at(10)
    visits = [booked(1, at(11)), walk_in(2, at(9, 55))]
    est = estimate_all(day(now), visits)
    assert est[2].wait_min == 0
    assert est[1].expected_start == at(11)


def test_checked_in_booked_patient_ordered_by_appointment():
    now = at(10)
    visits = [walk_in(1, at(9, 50)), booked(2, at(9, 45), checked_in=at(9, 55))]
    est = estimate_all(day(now), visits)
    assert est[2].position == 1
    assert est[1].position == 2


def test_overdue_booked_patient_does_not_block_waiting_ones():
    now = at(10)
    visits = [booked(1, at(9, 15)), walk_in(2, at(9, 50))]   # 45 min late, not arrived
    est = estimate_all(day(now), visits)
    assert est[2].wait_min == 0
    assert est[2].position == 1
    assert est[1].expected_start == now + timedelta(minutes=15)


def test_recently_due_booked_patient_still_holds_place():
    now = at(10)
    visits = [booked(1, at(9, 45)), walk_in(2, at(9, 50))]   # 15 min late: still expected
    est = estimate_all(day(now), visits)
    assert est[2].wait_min == pytest.approx(15)


# --- edge cases -------------------------------------------------------------

def test_zero_doctors_is_safe():
    now = at(10)
    visits = [walk_in(1, at(9, 50)), walk_in(2, at(9, 55))]
    est = estimate_all(day(now, doctors=0, expected=0), visits)
    assert est[2].wait_min == pytest.approx(15)
    est = estimate_all(day(now, opened=False, doctors=0, expected=0, opening=at(10)), visits)
    assert est[2].wait_min == pytest.approx(15)


def test_more_patients_in_visit_than_doctors():
    now = at(10)
    visits = [in_visit(1, at(9, 55)), in_visit(2, at(9, 50)), walk_in(3, at(9, 40))]
    est = estimate_all(day(now, doctors=1), visits)
    assert est[3].wait_min == pytest.approx(5)


def test_empty_queue_and_walk_in_estimate():
    now = at(10)
    assert estimate_all(day(now), []) == {}
    assert walk_in_estimate(day(now), []).wait_min == 0
    visits = [walk_in(1, at(9, 50)), walk_in(2, at(9, 55))]
    probe = walk_in_estimate(day(now), visits)
    assert probe.wait_min == pytest.approx(30)
    assert probe.position == 3


def test_done_and_cancelled_ignored():
    now = at(10)
    visits = [
        VisitInput(id=1, state="done", is_booked=False, appointment_at=None,
                   checked_in_at=at(9), started_at=at(9, 5)),
        VisitInput(id=2, state="cancelled", is_booked=True, appointment_at=at(9, 45),
                   checked_in_at=None, started_at=None),
        walk_in(3, at(9, 55)),
    ]
    est = estimate_all(day(now), visits)
    assert set(est) == {3}
    assert est[3].wait_min == 0
