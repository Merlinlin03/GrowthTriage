from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]

LOCAL_ENV_KEYS = {
    "GT_APP_MODE", "GT_DATABASE_URL", "GT_COOKIE_SECURE", "GT_GUEST_TTL_SECONDS",
    "GT_HARD_GRACE_SECONDS", "GT_ADMIN_PASSWORD_HASH", "GT_LIVE_DAILY_LIMIT",
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "GT_LLM_REQUEST_TIMEOUT_SECONDS",
}


def _load_local_env() -> None:
    """Load an ignored local config file without executing its contents."""
    path = PROJECT_ROOT / ".env.local"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in LOCAL_ENV_KEYS:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_local_env()


@dataclass(frozen=True)
class Settings:
    app_mode: str = os.getenv("GT_APP_MODE", "local").strip().lower()
    database_url: str = os.getenv(
        "GT_DATABASE_URL",
        f"sqlite:///{(PROJECT_ROOT / 'data' / 'growthtriage.db').as_posix()}",
    )
    cookie_secure: bool = os.getenv("GT_COOKIE_SECURE", "false").lower() == "true"
    guest_ttl_seconds: int = int(os.getenv("GT_GUEST_TTL_SECONDS", "7200"))
    hard_grace_seconds: int = int(os.getenv("GT_HARD_GRACE_SECONDS", "120"))
    deepseek_api_key: str | None = os.getenv("DEEPSEEK_API_KEY", "").strip() or None
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
    admin_password_hash: str | None = os.getenv("GT_ADMIN_PASSWORD_HASH")
    live_daily_limit: int = int(os.getenv("GT_LIVE_DAILY_LIMIT", "30"))
    llm_request_timeout_seconds: int = int(os.getenv("GT_LLM_REQUEST_TIMEOUT_SECONDS", "60"))
    frontend_dist: Path = PROJECT_ROOT / "frontend" / "dist"

    def validate_startup(self) -> None:
        if not 10 <= self.llm_request_timeout_seconds <= 180:
            raise RuntimeError("GT_LLM_REQUEST_TIMEOUT_SECONDS must be between 10 and 180")
        if self.app_mode not in {"local", "production"}:
            raise RuntimeError("GT_APP_MODE must be local or production")
        if self.app_mode == "production" and not self.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required in production mode")
        if self.app_mode == "production" and not self.deepseek_model:
            raise RuntimeError("DEEPSEEK_MODEL is required in production mode")
        if self.deepseek_api_key:
            parsed = urlparse(self.deepseek_base_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise RuntimeError("DEEPSEEK_BASE_URL must be a credential-free HTTPS URL")


settings = Settings()
