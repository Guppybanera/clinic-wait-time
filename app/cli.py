"""Command line:  python -m app.cli <command>

  create-staff --username U --name "Full Name" [--role manager|front_desk]
      Prompts for the password (or reads STAFF_PASSWORD from the environment).
  seed-demo
      DEV_MODE only. Fills today with demo bookings and walk-ins (fake names) and creates a
      demo manager login: username "demo", password "demo-password". Never use in production.
"""
from __future__ import annotations

import argparse
import getpass
import os
import random
import sys
from datetime import datetime, timedelta

from . import auth, clock, queue, snapshot
from .config import settings
from .db import SessionLocal, init_db
from .models import Visit

# Demo names: simple, generic first and last names (no caste or community surnames).
DEMO_NAMES = ["Anita Kumari", "Ravi Kumar", "Meena Devi", "Arun Raj", "Sunita Devi", "Vijay Kumar",
              "Rekha Rani", "Manoj Kumar", "Lata Devi", "Ajay Raj", "Kavita Rani", "Suresh Babu"]


def create_staff(args) -> int:
    pw = os.environ.get("STAFF_PASSWORD") or getpass.getpass("Password (8+ characters): ")
    with SessionLocal() as db:
        try:
            u = auth.create_user(db, username=args.username, name=args.name or args.username,
                                 password=pw, role=args.role)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"Created {u.role.replace('_', ' ')} '{u.username}'.")
    return 0


DEMO_USER, DEMO_PASSWORD = "demo", "demo-password"


def seed_demo(args) -> int:
    if not settings.dev_mode:
        print("seed-demo only runs with DEV_MODE=true.", file=sys.stderr)
        return 1
    rnd = random.Random(42)
    now = clock.now()
    with SessionLocal() as db:
        try:
            auth.create_user(db, username=DEMO_USER, name="Demo Manager", password=DEMO_PASSWORD,
                             role=auth.MANAGER)
            print(f"Created demo manager login '{DEMO_USER}' (password in app/cli.py).")
        except ValueError:
            pass
        if db.query(Visit).filter(Visit.visit_date == now.date()).count():
            print("Today already has visits; not seeding.")
            return 1
        cfg = snapshot.get_config(db)
        if not cfg.clinic_phone:
            cfg.clinic_phone = "+91 44 0000 0000"
        db.commit()
        names = DEMO_NAMES[:]
        rnd.shuffle(names)
        slot = timedelta(minutes=cfg.slot_length_min)
        first = (now + timedelta(minutes=20)).replace(second=0, microsecond=0)
        first = first.replace(minute=(first.minute // 15) * 15)
        queue.open_clinic(db, None)
        for i in range(6):
            queue.book(db, None, name=names.pop(), phone=f"98400{rnd.randint(10000, 99999)}",
                       appointment_at=first + i * slot * 2)
        tomorrow = datetime.combine(now.date() + timedelta(days=1), cfg.default_opening_time)
        for i in range(3):
            queue.book(db, None, name=names.pop(), phone=f"98400{rnd.randint(10000, 99999)}",
                       appointment_at=tomorrow + i * slot)
        walk_ins = [queue.walk_in(db, None, name=names.pop(), phone=f"98400{rnd.randint(10000, 99999)}")
                    for _ in range(3)]
        queue.move_to_queue(db, None, walk_ins[0].id)  # one already in the queue, two on the list
        print("Seeded today with 6 bookings and 3 walk-ins (1 in the queue, 2 on the walk-in list), "
              "and 3 bookings tomorrow.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-staff", help="add a staff login")
    c.add_argument("--username", required=True)
    c.add_argument("--name", default="")
    c.add_argument("--role", choices=[auth.FRONT_DESK, auth.MANAGER], default=auth.FRONT_DESK)
    c.set_defaults(fn=create_staff)
    s = sub.add_parser("seed-demo", help="fill today with demo data")
    s.set_defaults(fn=seed_demo)
    args = p.parse_args(argv)
    init_db()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
