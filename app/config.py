"""Runtime settings, all overridable with environment variables (see README)."""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .shops import DEFAULT_KEYS

log = logging.getLogger(__name__)
READERS = ("feed", "browser")  # how the shops are read: see app/shops.py and app/browser.py


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _names(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """A comma-separated list. Unset means the default; empty, "none" or "off" means nothing."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    names = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return () if names in {("none",), ("off",)} else names


def _reader() -> str:
    raw = os.environ.get("SHOP_READER", "").strip().lower()
    if raw and raw not in READERS:
        log.warning("SHOP_READER=%s is not one of %s; using %s", raw, ", ".join(READERS), READERS[0])
    return raw if raw in READERS else READERS[0]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    ha_webhook_url: str
    app_url: str  # where the web UI is reachable, linked from notifications
    poll_hours: int
    digest_time: str  # "HH:MM" in local TZ
    recheck_hours: int
    preorder_recheck_hours: int
    restock_watch_days: int
    max_page_fetches: int
    scan_pages: int
    seed_pages: int
    request_delay_min: float
    request_delay_max: float
    health_fail_threshold: int
    failure_backoff_seconds: float
    backlog_retry_minutes: int
    run_on_start: bool
    shops: tuple[str, ...]  # shops whose own listings are watched (keys from app/shops.py)
    shop_poll_minutes: int
    news_enabled: bool  # HD Report's steelbook headlines in the daily digest
    shop_reader: str = "feed"  # "browser" reads the shops with Chromium instead
    browser_path: str = ""  # a Chromium to use instead of the one Playwright installs

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
        preorder_recheck_hours=max(1, _int("PREORDER_RECHECK_HOURS", 24)),
        restock_watch_days=max(0, _int("RESTOCK_WATCH_DAYS", 30)),
        max_page_fetches=max(1, _int("MAX_PAGE_FETCHES", 40)),
        scan_pages=max(1, _int("SCAN_PAGES", 2)),
        seed_pages=max(1, _int("SEED_PAGES", 8)),
        request_delay_min=max(0.0, _float("REQUEST_DELAY_MIN", 4)),
        request_delay_max=max(0.0, _float("REQUEST_DELAY_MAX", 9)),
        health_fail_threshold=max(1, _int("HEALTH_FAIL_THRESHOLD", 3)),
        failure_backoff_seconds=max(0.0, _float("FAILURE_BACKOFF_SECONDS", 60)),
        backlog_retry_minutes=max(5, _int("BACKLOG_RETRY_MINUTES", 20)),
        run_on_start=_bool("RUN_ON_START", True),
        shops=_names("SHOPS", DEFAULT_KEYS),
        shop_poll_minutes=max(10, _int("SHOP_POLL_MINUTES", 60)),
        news_enabled=_bool("NEWS_ENABLED", True),
        shop_reader=_reader(),
        browser_path=os.environ.get("BROWSER_PATH", "").strip(),
    )
