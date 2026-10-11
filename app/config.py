"""Runtime settings, all overridable with environment variables (see README)."""

import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    ha_webhook_url: str
    app_url: str  # where the web UI is reachable, linked from notifications
    poll_hours: int
    digest_time: str  # "HH:MM" in local TZ
    recheck_hours: int
    max_page_fetches: int
    scan_pages: int
    seed_pages: int
    request_delay_min: float
    request_delay_max: float
    health_fail_threshold: int
    reddit_enabled: bool
    run_on_start: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tracker.db"


def load() -> Settings:
    return Settings(
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        ha_webhook_url=os.environ.get("HA_WEBHOOK_URL", "").strip(),
        app_url=os.environ.get("APP_URL", "").strip().rstrip("/"),
        poll_hours=max(1, _int("POLL_HOURS", 4)),
        digest_time=os.environ.get("DIGEST_TIME", "09:00"),
        recheck_hours=max(1, _int("RECHECK_HOURS", 12)),
        max_page_fetches=max(1, _int("MAX_PAGE_FETCHES", 40)),
        scan_pages=max(1, _int("SCAN_PAGES", 2)),
        seed_pages=max(1, _int("SEED_PAGES", 8)),
        request_delay_min=float(os.environ.get("REQUEST_DELAY_MIN", 4)),
        request_delay_max=float(os.environ.get("REQUEST_DELAY_MAX", 9)),
        health_fail_threshold=max(1, _int("HEALTH_FAIL_THRESHOLD", 3)),
        reddit_enabled=_bool("REDDIT_ENABLED", True),
        run_on_start=_bool("RUN_ON_START", True),
    )
