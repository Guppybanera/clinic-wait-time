"""Patient-facing endpoints. Nothing here ever reads or returns a name or full phone number."""
from __future__ import annotations

import base64
import hashlib
import hmac

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock, notifier, ratelimit, snapshot, sms
from ..config import settings
from ..crypto import phone_hash
from ..db import get_db
from ..models import BOOKED, DONE, IN_VISIT, WAITING, Visit
from .common import templates

router = APIRouter()

NOT_FOUND = {"kind": "not_found", "headline": "Link not found",
             "sub": "Please check the link in your text message.", "note": ""}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def patient_status(db: Session, token: str) -> dict:
    """What the patient page shows. Only position, times and the clinic's own details."""
    if not token or len(token) > 64:
        return NOT_FOUND
    v = db.scalar(select(Visit).where(Visit.link_token == token))
    if v is None:
        return NOT_FOUND
    cfg = snapshot.get_config(db)
    today = clock.now().date()
    base = {"clinic": cfg.clinic_name, "phone": cfg.clinic_phone, "note": ""}
    if v.visit_date < today or v.state not in (BOOKED, WAITING, IN_VISIT, DONE):
        return {**base, "kind": "expired", "headline": "This link has expired", "sub": ""}
    if v.state == DONE:
        return {**base, "kind": "done", "headline": "Your visit is complete", "sub": "Thank you."}
    if v.state == IN_VISIT:
        return {**base, "kind": "turn", "headline": "It's your turn",
                "sub": "Please come to the front desk."}
    if v.visit_date > today:
        when = v.appointment_at.strftime("%a %d %b") + " at " + notifier.clock_str(v.appointment_at)
        return {**base, "kind": "future", "headline": f"Appointment {when}",
                "sub": "We'll text you on the day with your expected start time."}
    e = snapshot.estimates(db).get(v.id)
    if e is None:
        return {**base, "kind": "expired", "headline": "This link has expired", "sub": ""}
    note = ""
    if snapshot.is_stale(db):
        note = "Estimate may be out of date" + (f" — call {cfg.clinic_phone}" if cfg.clinic_phone else "")
    if v.state == BOOKED:
        return {**base, "kind": "booked", "headline": f"Expected to be seen about {notifier.clock_range(e)}",
                "sub": "Estimate", "note": note}
    if e.walk_in_place:
        return {**base, "kind": "waiting",
                "headline": f"{notifier.ordinal(e.walk_in_place)} on the walk-in list",
                "sub": notifier.wait_str(e),
                "note": note or "Staff will add you to the queue; your time may change then."}
    return {**base, "kind": "waiting", "headline": f"{notifier.ordinal(e.position or 1)} in line",
            "sub": notifier.wait_str(e), "note": note}


@router.get("/w/{token}", response_class=HTMLResponse)
def patient_page(token: str, request: Request, db: Session = Depends(get_db)):
    if not ratelimit.patient_pages.allow(_client_ip(request)):
        return HTMLResponse("Too many requests. Please wait a minute.", status_code=429)
    s = patient_status(db, token)
    code = 404 if s["kind"] == "not_found" else 200
    resp = templates.TemplateResponse(request, "patient.html", {"s": s, "token": token}, status_code=code)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@router.get("/api/w/{token}")
def patient_api(token: str, request: Request, db: Session = Depends(get_db)):
    if not ratelimit.patient_pages.allow(_client_ip(request)):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    s = patient_status(db, token)
    return JSONResponse(s, status_code=404 if s["kind"] == "not_found" else 200,
                        headers={"Cache-Control": "no-store"})


def twilio_signature_ok(url: str, params: dict[str, str], signature: str, token: str) -> bool:
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(digest, signature or "")


@router.post("/api/sms/inbound")
async def sms_inbound(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    if settings.sms_backend == "twilio":
        url = settings.base_url.rstrip("/") + "/api/sms/inbound"
        if not twilio_signature_ok(url, params, request.headers.get("X-Twilio-Signature", ""),
                                   settings.twilio_auth_token):
            return Response(status_code=403)
    sender = params.get("From", "")
    empty = Response('<?xml version="1.0" encoding="UTF-8"?><Response/>', media_type="application/xml")
    if not sender:
        return empty
    if not ratelimit.inbound_sms.allow(phone_hash(sender)):
        return empty
    notifier.handle_inbound(db, sender, params.get("Body", ""))
    db.commit()
    sms.flush(db)
    return empty
