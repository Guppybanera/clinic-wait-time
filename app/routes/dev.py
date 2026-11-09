"""DEV_MODE only: see the simulated SMS outbox and pretend to text WAIT."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth, notifier, sms
from ..crypto import decrypt
from ..db import get_db
from ..models import Notification, StaffUser, Visit
from .common import back, flash, pop_flash, templates

router = APIRouter()


@router.get("/dev/outbox", response_class=HTMLResponse)
def outbox(request: Request, db: Session = Depends(get_db), user: StaffUser = Depends(auth.current_staff)):
    rows = list(db.scalars(select(Notification).order_by(Notification.id.desc()).limit(200)))
    items = []
    for n in rows:
        v = db.get(Visit, n.visit_id) if n.visit_id else None
        name = decrypt(v.patient_name_encrypted) if v and v.patient_name_encrypted else ""
        items.append({"n": n, "to": decrypt(n.to_encrypted), "name": name})
    return templates.TemplateResponse(request, "outbox.html", {
        "user": user, "items": items, "flash": pop_flash(request)})


@router.post("/dev/sms")
def fake_inbound(request: Request, phone: str = Form(...), body: str = Form("WAIT"),
                 db: Session = Depends(get_db), user: StaffUser = Depends(auth.current_staff)):
    reply = notifier.handle_inbound(db, phone, body)
    db.commit()
    sms.flush(db)
    flash(request, f"Reply: {reply}")
    return back("/dev/outbox")
