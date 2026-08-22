"""Environment-driven settings.

Everything an invigilator or deployer can change lives here, read from the environment
so Railway variables are the single source of truth. Nothing secret is ever committed.
"""

import os
import secrets
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- identity -----------------------------------------------------------
    app_name: str = "MBS6011 Test Platform"
    institution: str = "University of Zambia - Graduate School of Business"
    course_code: str = "MBS6011"
    course_title: str = "MBS6011: E-Business Strategies and Models"
    base_url: str = "http://127.0.0.1:8000"

    # --- database -----------------------------------------------------------
    # Railway injects DATABASE_URL for the Postgres addon. SQLite is the local default
    # so the app runs with no services attached.
    database_url: str = "sqlite:///./mbs6011_local.db"

    # --- security -----------------------------------------------------------
    # ADMIN_PASSWORD is required in production; the admin account is created or its
    # password reset from this value on every app start.
    admin_email: str = "admin@example.com"
    admin_password: str = ""
    secret_key: str = ""
    session_cookie: str = "mbs6011_session"
    session_max_age_seconds: int = 60 * 60 * 6
    verification_token_max_age_seconds: int = 60 * 60 * 48

    # Off by default: students self-register ahead of the sitting and the emailed link
    # is the gate. Turn on to make the roster an allowlist you approve by hand.
    require_admin_approval: bool = False
    # Optional guard on who may register at all, e.g. "unza.zm,cs.unza.zm".
    allowed_email_domains: str = ""

    # --- email --------------------------------------------------------------
    mail_backend: str = "console"  # console | smtp | resend
    mail_from: str = "MBS6011 Test Platform <no-reply@example.com>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    resend_api_key: str = ""

    # --- proctoring ---------------------------------------------------------
    absence_warn_seconds: int = 20
    absence_flag_seconds: int = 60
    multiple_faces_seconds: int = 6
    phone_confirm_seconds: float = 0.75
    phone_warning_display_seconds: int = 45
    fullscreen_grace_seconds: int = 10
    hidden_warn_seconds: int = 15
    strike_flag_after: int = 3
    block_shortcut_keys: bool = True
    block_copy_paste: bool = True
    block_context_menu: bool = True

    # Evidence snapshots
    snapshots_enabled: bool = True
    snapshot_max_width: int = 480
    snapshot_alert_min_interval_seconds: int = 180
    snapshot_max_per_attempt: int = 40
    server_side_snapshot_recheck: bool = True

    # --- misc ---------------------------------------------------------------
    clock_backwards_tolerance_seconds: int = 120
    log_retention_days: int = 365

    @property
    def sqlalchemy_url(self) -> str:
        """Railway hands out postgres:// which SQLAlchemy 2 no longer accepts."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url

    @property
    def is_postgres(self) -> bool:
        return self.sqlalchemy_url.startswith("postgresql")

    @property
    def allowed_domains(self) -> list[str]:
        return [d.strip().lower() for d in self.allowed_email_domains.split(",") if d.strip()]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.secret_key:
        # A generated key is fine for a single container, but it rotates on restart and
        # would sign every session out. Warn loudly rather than failing to boot.
        settings.secret_key = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(48)
    return settings


settings = get_settings()
