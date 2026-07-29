"""Application configuration with safe, dependency-free environment loading."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, replace
from ipaddress import ip_address
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.getenv("LOCALAPPDATA", str(Path.home())))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return root / "jobsearch-ai-assistant"


def _default_web_dir() -> Path:
    relative = Path("share") / "jobsearch-ai-assistant" / "web"
    candidates = [PROJECT_ROOT / "web"]
    candidates.extend(parent / relative for parent in Path(__file__).resolve().parents)
    candidates.append(Path(sys.prefix) / relative)
    for candidate in candidates:
        if candidate.joinpath("index.html").is_file():
            return candidate
    return candidates[-1]


def _default_env_file() -> Path:
    working_copy = Path.cwd() / ".env"
    return working_copy if working_copy.is_file() else PROJECT_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    """Load a simple .env file without overriding process environment values."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalised = value.strip().lower()
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: 1/0, true/false, yes/no, on/off")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _allowed_hosts(value: str) -> tuple[str, ...]:
    hosts: list[str] = []
    for raw_host in value.split(","):
        raw_host = raw_host.strip()
        if not raw_host:
            continue
        candidate = (
            raw_host[1:-1] if raw_host.startswith("[") and raw_host.endswith("]") else raw_host
        )
        candidate = candidate.lower().rstrip(".")
        try:
            ip_address(candidate)
        except ValueError:
            if not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                candidate,
            ):
                raise ValueError(
                    "ALLOWED_HOSTS entries must be hostnames or IP addresses without ports"
                ) from None
        hosts.append(candidate)
    if not hosts:
        raise ValueError("ALLOWED_HOSTS must contain at least one host")
    return tuple(dict.fromkeys(hosts))


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings. Defaults are safe for a local desktop application."""

    host: str
    port: int
    data_dir: Path
    web_dir: Path
    max_body_bytes: int
    rate_limit_per_minute: int
    max_concurrent_requests: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    log_level: str
    demo_enabled: bool
    store_raw_resume: bool
    ai_api_key: str
    ai_base_url: str
    ai_model: str
    ai_timeout_seconds: int
    serpapi_key: str
    scheduler_enabled: bool
    background_workers_enabled: bool
    worker_poll_seconds: int
    worker_lease_seconds: int
    timezone: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_use_tls: bool

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Settings:
        _load_env_file(env_file or _default_env_file())
        origins = tuple(
            origin.strip().rstrip("/")
            for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        )
        allowed_hosts = _allowed_hosts(
            os.getenv(
                "ALLOWED_HOSTS",
                "127.0.0.1,localhost,::1",
            )
        )
        return cls(
            host=os.getenv("APP_HOST", "127.0.0.1"),
            port=_env_int("APP_PORT", 3000, 0, 65535),
            data_dir=Path(os.getenv("APP_DATA_DIR", str(_default_data_dir()))).resolve(),
            web_dir=Path(os.getenv("APP_WEB_DIR", str(_default_web_dir()))).resolve(),
            max_body_bytes=_env_int(
                "MAX_REQUEST_BYTES", 6 * 1024 * 1024, 64 * 1024, 20 * 1024 * 1024
            ),
            rate_limit_per_minute=_env_int("RATE_LIMIT_PER_MINUTE", 120, 10, 10_000),
            max_concurrent_requests=_env_int("MAX_CONCURRENT_REQUESTS", 24, 2, 256),
            allowed_hosts=allowed_hosts,
            allowed_origins=origins,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            demo_enabled=_env_bool("DEMO_ENABLED", True),
            store_raw_resume=_env_bool("STORE_RAW_RESUME", False),
            ai_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            ai_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            ai_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            ai_timeout_seconds=_env_int("AI_TIMEOUT_SECONDS", 45, 5, 180),
            serpapi_key=os.getenv("SERPAPI_KEY", "").strip(),
            scheduler_enabled=_env_bool("SCHEDULER_ENABLED", True),
            background_workers_enabled=_env_bool("BACKGROUND_WORKERS_ENABLED", True),
            worker_poll_seconds=_env_int("WORKER_POLL_SECONDS", 2, 1, 60),
            worker_lease_seconds=_env_int("WORKER_LEASE_SECONDS", 900, 60, 3600),
            timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
            smtp_host=os.getenv("SMTP_HOST", "").strip(),
            smtp_port=_env_int("SMTP_PORT", 587, 1, 65535),
            smtp_user=os.getenv("SMTP_USER", "").strip(),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_from=os.getenv("SMTP_FROM", "").strip(),
            smtp_use_tls=_env_bool("SMTP_USE_TLS", True),
        )

    def with_overrides(self, **kwargs: object) -> Settings:
        return replace(self, **kwargs)
