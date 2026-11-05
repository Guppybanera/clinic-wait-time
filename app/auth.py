"""Staff logins: individual users, bcrypt passwords, idle lock."""
from __future__ import annotations

from datetime import datetime, timedelta

import bcrypt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock, snapshot
from .db import get_db
from .models import StaffUser

MANAGER = "manager"
FRONT_DESK = "front_desk"


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/staff", locked: bool = False):
        self.next_url = next_url
        self.locked = locked


class Forbidden(Exception):
    pass


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def create_user(db: Session, *, username: str, name: str, password: str, role: str = FRONT_DESK) -> StaffUser:
    username = username.strip().lower()
    if not username or len(password) < 8:
        raise ValueError("Username required and password must be at least 8 characters")
    if role not in (MANAGER, FRONT_DESK):
        raise ValueError("Unknown role")
    if db.scalar(select(StaffUser).where(StaffUser.username == username)):
        raise ValueError("That username is taken")
    u = StaffUser(username=username, name=name.strip() or username, password_hash=hash_password(password),
                  role=role, active=True)
    db.add(u)
    db.commit()
    return u


def authenticate(db: Session, username: str, password: str) -> StaffUser | None:
    u = db.scalar(select(StaffUser).where(StaffUser.username == username.strip().lower()))
    if u is None or not u.active or not verify_password(password, u.password_hash):
        # Spend similar time whether or not the user exists.
        if u is None:
            verify_password(password, "$2b$12$" + "." * 53)
        return None
    return u


def log_in(request: Request, user: StaffUser) -> None:
    request.session.clear()
    request.session["uid"] = user.id
    request.session["seen"] = clock.now().isoformat()


def log_out(request: Request) -> None:
    request.session.clear()


def current_staff(request: Request, db: Session = Depends(get_db)) -> StaffUser:
    """Signed-in staff user. Locks after `idle_lock_min` without a real (non-auto-refresh) request."""
    next_url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    uid = request.session.get("uid")
    if not uid:
        raise LoginRequired(next_url)
    user = db.get(StaffUser, uid)
    if user is None or not user.active:
        request.session.clear()
        raise LoginRequired(next_url)
    now = clock.now()
    try:
        seen = datetime.fromisoformat(request.session.get("seen", ""))
    except ValueError:
        seen = datetime.min
    idle = timedelta(minutes=snapshot.get_config(db).idle_lock_min)
    if now - seen > idle:
        request.session.clear()
        raise LoginRequired(next_url.replace("auto=1", "").rstrip("?&"), locked=True)
    if request.query_params.get("auto") != "1":
        request.session["seen"] = now.isoformat()
    return user


def manager(user: StaffUser = Depends(current_staff)) -> StaffUser:
    if user.role != MANAGER:
        raise Forbidden()
    return user
