"""Encryption for patient name and phone, plus a keyed hash for matching inbound SMS."""
import base64
import hashlib
import hmac
import re

from cryptography.fernet import Fernet

from .config import settings


def _fernet() -> Fernet:
    key = settings.data_key
    if not key:
        # Dev fallback: derive a stable key from SECRET_KEY. Production must set DATA_KEY.
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()).decode()
    return Fernet(key.encode())


def encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet().decrypt(value.encode()).decode()


def normalize_phone(raw: str) -> str:
    """Keep digits and a leading +, so '+91 98400-12345' and '+919840012345' match."""
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    return ("+" + digits) if raw.startswith("+") else digits


def phone_hash(phone: str) -> str:
    """Keyed hash of the last 10 digits, so the same number matches with or without a country code."""
    digits = re.sub(r"\D", "", phone)[-10:]
    return hmac.new(settings.secret_key.encode(), digits.encode(), hashlib.sha256).hexdigest()


def last4(phone: str) -> str:
    return re.sub(r"\D", "", phone)[-4:]
