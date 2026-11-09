"""Shared test setup."""

import asyncio
import os
import tempfile
from datetime import date

import pytest

# app.main opens its database when it is imported, so point it somewhere disposable first.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="steelbook-tests-")

import app.jobs  # noqa: E402
import app.main  # noqa: E402

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


@pytest.fixture
def no_waiting(monkeypatch):
    """The Blu-ray.com client waits 30 s before retrying a refused request; skip that."""
    real_sleep = asyncio.sleep

    async def instant(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant)
