from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..crypto import decrypt
from ..models import Visit

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def hm(t: datetime | None) -> str:
    return t.strftime("%H:%M") if t else ""


def full_name(v: Visit) -> str:
    try:
        return decrypt(v.patient_name_encrypted) or "(name deleted)"
    except Exception:  # noqa: BLE001
        return "(unreadable)"


def short_name(v: Visit) -> str:
    """'Ravi Kumar' -> 'Ravi K.' (as on the paper book)."""
    parts = full_name(v).split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[-1][0]}."
    return parts[0] if parts else ""


def phone_end(v: Visit) -> str:
    return f"…{v.phone_last4}" if v.phone_last4 else ""


def full_phone(v: Visit) -> str:
    try:
        return decrypt(v.phone_encrypted) or ""
    except Exception:  # noqa: BLE001
        return ""


def is_manager(user) -> bool:
    return getattr(user, "role", None) == "manager"


def name_for(v: Visit, user) -> str:
    """Managers see the full name everywhere; front desk sees 'Ravi K.' on the book."""
    return full_name(v) if is_manager(user) else short_name(v)


def phone_for(v: Visit, user) -> str:
    """Managers see the complete number; front desk sees the last 4 digits."""
    return (full_phone(v) or phone_end(v)) if is_manager(user) else phone_end(v)


templates.env.filters["hm"] = hm
templates.env.globals.update(full_name=full_name, short_name=short_name, phone_end=phone_end,
                             name_for=name_for, phone_for=phone_for, is_manager=is_manager,
                             dev_mode=settings.dev_mode)


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session["flash"] = [kind, msg]


def pop_flash(request: Request):
    return request.session.pop("flash", None)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
