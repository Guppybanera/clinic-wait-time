"""Database tables. Matches the 'Data model' section of the technical spec."""
from datetime import date, datetime, time

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# Visit states
BOOKED = "booked"
WAITING = "waiting"
IN_VISIT = "in_visit"
DONE = "done"
NO_SHOW = "no_show"
CANCELLED = "cancelled"
ACTIVE_STATES = (BOOKED, WAITING, IN_VISIT)

# Visit types
TYPE_BOOKED = "booked"
TYPE_WALK_IN = "walk_in"


class Visit(Base):
    __tablename__ = "visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_date: Mapped[date] = mapped_column(Date, index=True)
    patient_name_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    phone_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    appointment_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    type: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(10), index=True)
    link_token: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)
    pre_arrival_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Walk-ins: when a manager moved them from the walk-in list into the appointment queue.
    queued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_estimate_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_wait_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Expected start time the patient was last told (by any text). Delay alerts compare against it.
    last_notified_start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_delay_text_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class ClinicDay(Base):
    __tablename__ = "clinic_days"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    opening_time: Mapped[time] = mapped_column(Time)
    closing_time: Mapped[time] = mapped_column(Time)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set by "Running late": no patient is expected to be seen before this time.
    delay_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    doctors_expected: Mapped[int] = mapped_column(Integer)
    doctors_in_session: Mapped[int] = mapped_column(Integer)
    last_staff_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ClinicConfig(Base):
    """Single row (id=1) of settings the manager can change without code."""

    __tablename__ = "clinic_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clinic_name: Mapped[str] = mapped_column(String(100), default="Community Health Clinic")
    clinic_phone: Mapped[str] = mapped_column(String(30), default="")
    default_visit_min: Mapped[int] = mapped_column(Integer, default=15)
    slot_length_min: Mapped[int] = mapped_column(Integer, default=15)
    default_opening_time: Mapped[time] = mapped_column(Time, default=time(9, 0))
    default_closing_time: Mapped[time] = mapped_column(Time, default=time(17, 0))
    default_doctors: Mapped[int] = mapped_column(Integer, default=1)
    pre_arrival_lead_min: Mapped[int] = mapped_column(Integer, default=90)
    alert_threshold_min: Mapped[int] = mapped_column(Integer, default=15)
    quiet_start: Mapped[time] = mapped_column(Time, default=time(20, 0))
    quiet_end: Mapped[time] = mapped_column(Time, default=time(7, 0))
    name_retention_days: Mapped[int] = mapped_column(Integer, default=30)
    timing_retention_days: Mapped[int] = mapped_column(Integer, default=90)
    idle_lock_min: Mapped[int] = mapped_column(Integer, default=5)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_id: Mapped[int | None] = mapped_column(ForeignKey("visits.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(12))  # booking / pre_arrival / check_in / queued / delay / reply / adhoc
    # Encrypted destination, so the outbox can retry without keeping a plain number.
    to_encrypted: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(8), default="queued")  # queued / sent / failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_user_id: Mapped[int | None] = mapped_column(ForeignKey("staff_users.id"), nullable=True)
    visit_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(30))
    from_state: Mapped[str | None] = mapped_column(String(10), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(10), nullable=True)
    undone: Mapped[bool] = mapped_column(Boolean, default=False)
    at: Mapped[datetime] = mapped_column(DateTime)


class StaffUser(Base):
    __tablename__ = "staff_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(10), default="front_desk")  # front_desk / manager
    active: Mapped[bool] = mapped_column(Boolean, default=True)
