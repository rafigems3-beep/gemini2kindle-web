"""Centralized environment configuration for Gemini2Kindle Web."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env_str(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val.strip() if val is not None else default


def _env_int(key: str, default: int, min_val: int | None = None, max_val: int | None = None) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        return default
    if min_val is not None:
        parsed = max(min_val, parsed)
    if max_val is not None:
        parsed = min(max_val, parsed)
    return parsed


@dataclass(frozen=True)
class Settings:
    app_pin: str
    math_engine: str
    math_dpi: int
    math_node_timeout: int
    mail_mode: str
    kindle_email: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    apps_script_url: str
    apps_script_token: str
    default_author: str
    default_language: str
    max_markdown_chars: int
    max_upload_mb: int

    @property
    def is_mail_configured(self) -> bool:
        if not self.kindle_email:
            return False
        if self.mail_mode == "smtp":
            return bool(self.smtp_user and self.smtp_password)
        if self.mail_mode == "apps_script":
            return bool(self.apps_script_url and self.apps_script_token)
        return False


def load_settings() -> Settings:
    """Read current environment variables into a typed Settings instance."""
    math_engine = _env_str("MATH_ENGINE", "auto").lower()
    if math_engine not in {"auto", "mathjax", "matplotlib"}:
        math_engine = "auto"

    mail_mode = _env_str("MAIL_MODE", "smtp").lower()
    if mail_mode not in {"smtp", "apps_script"}:
        mail_mode = "smtp"

    return Settings(
        app_pin=_env_str("APP_PIN", ""),
        math_engine=math_engine,
        math_dpi=_env_int("MATH_DPI", 300, min_val=150, max_val=600),
        math_node_timeout=_env_int("MATH_NODE_TIMEOUT", 45, min_val=5, max_val=300),
        mail_mode=mail_mode,
        kindle_email=_env_str("KINDLE_EMAIL", ""),
        smtp_host=_env_str("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=_env_int("SMTP_PORT", 465, min_val=1, max_val=65535),
        smtp_user=_env_str("SMTP_USER", ""),
        smtp_password=_env_str("SMTP_PASSWORD", ""),
        apps_script_url=_env_str("APPS_SCRIPT_URL", ""),
        apps_script_token=_env_str("APPS_SCRIPT_TOKEN", ""),
        default_author=_env_str("DEFAULT_AUTHOR", "Gemini"),
        default_language=_env_str("DEFAULT_LANGUAGE", "id"),
        max_markdown_chars=_env_int("MAX_MARKDOWN_CHARS", 1_500_000, min_val=10_000, max_val=5_000_000),
        max_upload_mb=_env_int("MAX_UPLOAD_MB", 20, min_val=1, max_val=25),
    )
