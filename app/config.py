"""Environment-driven settings for the platform itself.

This is what the person who deploys/operates the running app sets - not any
one course. Course identity (code, title, institution) and which database
holds a course's data are configured per-lecturer, in the platform database,
via /signup and /{slug}/admin/setup - see app/models_platform.py. Nothing
here names a particular course.
"""

import os
import secrets
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- identity -------------------------------------------------------------
    app_name: str = "ClearGrade"
    base_url: str = "http://127.0.0.1:8000"

    # --- platform database ------------------------------------------------------
    # Holds lecturer accounts and their course settings - never course data itself.
    # Railway injects DATABASE_URL for the Postgres addon. SQLite is the local
    # default so the app runs with no services attached.
    database_url: str = "sqlite:///./platform.db"

    # --- security ---------------------------------------------------------------
    secret_key: str = ""
    session_cookie: str = "exam_session"
    session_max_age_seconds: int = 60 * 60 * 6
    verification_token_max_age_seconds: int = 60 * 60 * 48

    # Encrypts a lecturer's stored database connection string. Deliberately
    # separate from secret_key - see app/tenant_crypto.py.
    credential_encryption_key: str = ""

    # --- registration (per-course default, a lecturer's students opt into this) --
    require_admin_approval: bool = False
    allowed_email_domains: str = ""

    # --- email --------------------------------------------------------------
    mail_backend: str = "console"  # console | smtp | resend
    mail_from: str = "ClearGrade <no-reply@example.com>"
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
        return normalise_database_url(self.database_url)

    @property
    def is_postgres(self) -> bool:
        return self.sqlalchemy_url.startswith("postgresql")

    @property
    def allowed_domains(self) -> list[str]:
        return [d.strip().lower() for d in self.allowed_email_domains.split(",") if d.strip()]


def normalise_database_url(url: str) -> str:
    """Shared by the platform engine and every dynamically-created tenant engine."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.secret_key:
        # A generated key is fine for a single container, but it rotates on restart and
        # would sign every session out. Warn loudly rather than failing to boot.
        settings.secret_key = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(48)
    return settings


settings = get_settings()
