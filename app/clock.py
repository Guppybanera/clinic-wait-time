"""Single source of 'now', in the clinic's local time (naive datetimes).

The app serves one clinic, so every datetime is stored and compared as clinic-local
wall-clock time. Tests freeze time with set_now().
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings

_frozen: datetime | None = None


def now() -> datetime:
    if _frozen is not None:
        return _frozen
    return datetime.now(ZoneInfo(settings.clinic_tz)).replace(tzinfo=None, microsecond=0)


def set_now(value: datetime | None) -> None:
    global _frozen
    _frozen = value
