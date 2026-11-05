"""Background job.

Every 60 seconds (`tick`): recompute today's estimates (time passing can change them),
send due pre-arrival and delay texts, and retry queued texts.

Nightly (`nightly`, first tick after midnight and at startup): clear link tokens from past
days, delete names and phones after `name_retention_days`, delete visits after
`timing_retention_days`, notifications after 30 days and audit entries after a year.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from . import clock, queue, snapshot
from .db import SessionLocal
from .models import AuditLog, Notification, Visit

log = logging.getLogger(__name__)

NOTIFICATION_RETENTION = timedelta(days=30)
AUDIT_RETENTION = timedelta(days=365)


def tick(db: Session) -> None:
    queue.recompute(db)   # recomputes, sends due texts, commits, flushes the outbox


def nightly(db: Session) -> dict[str, int]:
    cfg = snapshot.get_config(db)
    now = clock.now()
    today = now.date()
    out: dict[str, int] = {}

    out["tokens_cleared"] = db.execute(
        update(Visit).where(Visit.visit_date < today, Visit.link_token.is_not(None))
        .values(link_token=None)).rowcount

    name_cutoff: date = today - timedelta(days=cfg.name_retention_days)
    out["names_deleted"] = db.execute(
        update(Visit).where(Visit.visit_date < name_cutoff, Visit.patient_name_encrypted.is_not(None)
                            | Visit.phone_encrypted.is_not(None))
        .values(patient_name_encrypted=None, phone_encrypted=None, phone_hash=None)).rowcount

    visit_cutoff: date = today - timedelta(days=cfg.timing_retention_days)
    old_ids = list(db.scalars(select(Visit.id).where(Visit.visit_date < visit_cutoff)))
    if old_ids:
        db.execute(update(Notification).where(Notification.visit_id.in_(old_ids)).values(visit_id=None))
        db.execute(delete(Visit).where(Visit.id.in_(old_ids)))
    out["visits_deleted"] = len(old_ids)

    out["notifications_deleted"] = db.execute(
        delete(Notification).where(Notification.created_at < now - NOTIFICATION_RETENTION)).rowcount
    out["audit_deleted"] = db.execute(
        delete(AuditLog).where(AuditLog.at < now - AUDIT_RETENTION)).rowcount
    db.commit()
    return out


def accuracy(db: Session, days: int = 7) -> tuple[int, float | None]:
    """Share of visits in the last `days` days whose first estimate was within 15 min of the actual wait."""
    since = clock.now().date() - timedelta(days=days - 1)
    rows = db.execute(select(Visit.first_estimate_min, Visit.actual_wait_min).where(
        Visit.visit_date >= since, Visit.first_estimate_min.is_not(None), Visit.actual_wait_min.is_not(None))).all()
    if not rows:
        return 0, None
    ok = sum(1 for est, act in rows if abs(est - act) <= 15)
    return len(rows), 100 * ok / len(rows)


async def run_forever(interval: float = 60) -> None:
    last_nightly: date | None = None
    while True:
        try:
            with SessionLocal() as db:
                today = clock.now().date()
                if last_nightly != today:
                    result = nightly(db)
                    log.info("nightly cleanup: %s", result)
                    last_nightly = today
                tick(db)
        except Exception:  # noqa: BLE001 - keep the loop alive; the next tick retries
            log.exception("background job failed")
        await asyncio.sleep(interval)

