"""SMS outbox and delivery backends.

Every text is first written to the `notifications` table (the outbox), then delivered.
Failed sends stay `queued` and are retried by the background job for up to 30 minutes.

Backends:
- "simulated" (default): marks the text sent; the body is visible at /dev/outbox.
- "twilio": sends through Twilio's REST API.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock
from .config import settings
from .crypto import decrypt, encrypt
from .models import Notification

log = logging.getLogger(__name__)

RETRY_WINDOW = timedelta(minutes=30)
QUEUED, SENT, FAILED = "queued", "sent", "failed"


class SmsError(Exception):
    pass


class SimulatedBackend:
    def send(self, to: str, body: str) -> None:
        return None


class TwilioBackend:
    def __init__(self, sid: str, token: str, from_number: str):
        self.sid, self.token, self.from_number = sid, token, from_number

    def send(self, to: str, body: str) -> None:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        try:
            r = httpx.post(url, data={"To": to, "From": self.from_number, "Body": body},
                           auth=(self.sid, self.token), timeout=10)
        except httpx.HTTPError as e:
            raise SmsError(type(e).__name__) from e
        if r.status_code >= 300:
            raise SmsError(f"twilio status {r.status_code}")


_backend_override = None


def backend():
    if _backend_override is not None:
        return _backend_override
    if settings.sms_backend == "twilio":
        return TwilioBackend(settings.twilio_account_sid, settings.twilio_auth_token,
                             settings.twilio_from_number)
    return SimulatedBackend()


def set_backend(b) -> None:
    """For tests: replace the delivery backend (None restores the configured one)."""
    global _backend_override
    _backend_override = b


# Characters outside the GSM 7-bit alphabet force the whole text into UCS-2, which fits 70
# characters per segment instead of 160 (153 when split), so a 300-character text bills as
# 5 messages instead of 2. Swap the common typographic ones for plain equivalents.
GSM_SWAPS = str.maketrans({"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
                           "…": "...", "\u00a0": " "})


def gsm_text(body: str) -> str:
    return body.translate(GSM_SWAPS)


def enqueue(db: Session, *, to: str, body: str, kind: str, visit_id: int | None) -> Notification:
    n = Notification(visit_id=visit_id, kind=kind, to_encrypted=encrypt(to), body=gsm_text(body),
                     status=QUEUED, attempts=0, created_at=clock.now())
    db.add(n)
    db.flush()
    return n


def deliver(db: Session, n: Notification) -> None:
    n.attempts += 1
    try:
        backend().send(decrypt(n.to_encrypted), n.body)
    except Exception as e:  # noqa: BLE001 - any backend failure leaves the text queued
        n.error = str(e)[:200]
        if clock.now() - n.created_at >= RETRY_WINDOW:
            n.status = FAILED
        # The log never includes the number or body.
        log.warning("SMS %s (kind=%s) not sent: %s", n.id, n.kind, n.error)
        return
    n.status = SENT
    n.sent_at = clock.now()
    n.error = None


def flush(db: Session) -> int:
    """Try every queued text once. Gives up on texts older than the retry window."""
    pending = db.scalars(select(Notification).where(Notification.status == QUEUED)
                        .order_by(Notification.id)).all()
    for n in pending:
        deliver(db, n)
    db.commit()
    return len(pending)


def texts_delayed(db: Session) -> bool:
    """True when some text has been waiting more than 2 minutes (shown on the console)."""
    cutoff = clock.now() - timedelta(minutes=2)
    return db.scalar(select(Notification.id).where(Notification.status == QUEUED,
                                                   Notification.created_at < cutoff).limit(1)) is not None
