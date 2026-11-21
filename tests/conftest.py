"""Shared test setup."""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import pytest

# app.main opens its database when it is imported, so point it somewhere disposable first.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="steelbook-tests-")
# Messages name times in the container's timezone; the tests expect UTC wherever they run.
os.environ["TZ"] = "UTC"
time.tzset()

import app.browser  # noqa: E402
import app.jobs  # noqa: E402
import app.main  # noqa: E402
import app.shops  # noqa: E402

TODAY = date(2026, 10, 11)  # the day the Blu-ray.com fixtures were captured


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    """Pin "today". The fixtures hold real release dates (Die Hard: Dec 1, 2026), and
    the scanner skips anything already released, so without this the suite would
    start failing the day after the first of those dates."""

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(app.jobs, "date", FrozenDate)
    monkeypatch.setattr(app.main, "date", FrozenDate)


@pytest.fixture(autouse=True)
def no_gap_between_shops(monkeypatch):
    """The shops are read a few seconds apart and never twice within a few minutes.
    Most tests read them back to back; the tests of those rules set them themselves."""
    monkeypatch.setattr(app.shops, "SHOP_GAP", 0)
    monkeypatch.setattr(app.shops, "MIN_GAP", 0)
    monkeypatch.setattr(app.browser, "SHOP_GAP", (0, 0))


@pytest.fixture(scope="session")
def chromium() -> str:
    """For the tests that run a real browser: the Chromium to start. That is BROWSER_PATH
    when it is set, otherwise the one ``python -m playwright install chromium`` puts in
    place (the empty string tells the reader to use that).

    Without a Chromium these tests are skipped, unless REQUIRE_BROWSER is set, as it is
    in CI: there a missing browser is a failure, not a reason to test less.
    """
    path = os.environ.get("BROWSER_PATH", "")
    if path:
        found = Path(path).exists()
    else:
        ask = subprocess.run(
            [sys.executable, "-c", "from playwright.sync_api import sync_playwright\n"
                                   "with sync_playwright() as p: print(p.chromium.executable_path)"],
            capture_output=True, text=True)
        found = ask.returncode == 0 and Path(ask.stdout.strip()).exists()
    if not found:
        message = "no Chromium to test with: run `python -m playwright install chromium` or set BROWSER_PATH"
        if os.environ.get("REQUIRE_BROWSER"):
            pytest.fail(message)
        pytest.skip(message)
    return path


@pytest.fixture
def no_waiting(monkeypatch):
    """The Blu-ray.com client waits 30 s before retrying a refused request; skip that."""
    real_sleep = asyncio.sleep

    async def instant(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant)
