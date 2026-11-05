import os
from datetime import datetime

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("RUN_JOBS", "false")
os.environ.setdefault("DEV_MODE", "true")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import clock, sms  # noqa: E402
from app.db import init_db  # noqa: E402


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(bind=eng)
    return eng


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s


@pytest.fixture(autouse=True)
def frozen_time():
    clock.set_now(datetime(2026, 11, 5, 9, 0))
    yield
    clock.set_now(None)
    sms.set_backend(None)


class FakeBackend:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def send(self, to, body):
        if self.fail:
            raise RuntimeError("provider down")
        self.sent.append((to, body))


@pytest.fixture
def phone():
    b = FakeBackend()
    sms.set_backend(b)
    return b
