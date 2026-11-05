"""Settings read from environment variables (see .env.example)."""
import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./clinic.db"))
    # Signs staff session cookies. MUST be set to a long random value in production.
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "dev-secret-change-me"))
    # Fernet key used to encrypt patient names and phone numbers at rest.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    data_key: str = field(default_factory=lambda: os.getenv("DATA_KEY", ""))
    clinic_tz: str = field(default_factory=lambda: os.getenv("CLINIC_TZ", "Asia/Kolkata"))
    # "simulated" stores texts in the outbox and shows them at /dev/outbox. "twilio" sends real SMS.
    sms_backend: str = field(default_factory=lambda: os.getenv("SMS_BACKEND", "simulated"))
    twilio_account_sid: str = field(default_factory=lambda: os.getenv("TWILIO_ACCOUNT_SID", ""))
    twilio_auth_token: str = field(default_factory=lambda: os.getenv("TWILIO_AUTH_TOKEN", ""))
    twilio_from_number: str = field(default_factory=lambda: os.getenv("TWILIO_FROM_NUMBER", ""))
    base_url: str = field(default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8000"))
    dev_mode: bool = field(default_factory=lambda: _bool("DEV_MODE", True))
    run_jobs: bool = field(default_factory=lambda: _bool("RUN_JOBS", True))
    https_only_cookies: bool = field(default_factory=lambda: _bool("HTTPS_ONLY_COOKIES", False))


settings = Settings()
