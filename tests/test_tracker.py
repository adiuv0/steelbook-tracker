"""End-to-end scan against mocked Blu-ray.com, Reddit and Home Assistant."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from app import config
from app.db import DB
from app.jobs import Tracker

FIX = Path(__file__).parent / "fixtures"
HA = "http://ha.local:8123/api/webhook/steelbooks"


def read(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = replace(config.load(), ha_webhook_url=HA, request_delay_min=0, request_delay_max=0,
                seed_pages=1, scan_pages=1)
    return Tracker(s, DB(s.db_path))


def mock_sites(router, die_hard_page: str):
    router.get("https://www.blu-ray.com/").respond(200, text="<html>home</html>")
    router.get(url__startswith="https://www.blu-ray.com/movies/search.php").respond(
        200, text=read("search_simple.html"))
    router.get("https://www.blu-ray.com/movies/Die-Hard-4K-Blu-ray/422773/").respond(200, text=die_hard_page)
    router.get("https://www.blu-ray.com/movies/Other-Mommy-4K-Blu-ray/422840/").respond(
        200, text=read("product_announced.html").replace("Die Hard", "Other Mommy"))
    router.get("https://www.blu-ray.com/movies/The-Burbs-4K-Blu-ray/421959/").respond(
        200, text=read("product_not_steelbook.html"))
    router.get("https://www.reddit.com/r/steelbooks/new/.rss").respond(200, text=read("reddit.xml"))
    return router.post(HA).respond(200)


def sent(ha_route) -> list[dict]:
    return [json.loads(c.request.content) for c in ha_route.calls]


@pytest.mark.asyncio
async def test_seed_then_preorder_alert_then_digest(tracker):
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, read("product_announced.html"))
        result = await tracker.run_scan()
        assert result.startswith("Seeded"), result
        # Seeding sends one setup message, never a flood of alerts.
        assert [p["type"] for p in sent(ha)] == ["setup"]

    # Old '80s Horror' release (Oct 6) is in the past and skipped; Burbs is not a steelbook.
    rows = tracker.db.list_releases("all", "2026-10-11")
    assert {r["id"] for r in rows} == {422840, 422773}

    tracker.db.set_meta("seeded", "x")
    # Force a recheck of Die Hard now that its preorder opened.
    with tracker.db.conn() as c:
        c.execute("UPDATE releases SET last_checked='2000-01-01' WHERE id=422773")
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, read("product_preorder.html").replace("Braveheart", "Die Hard"))
        await tracker.run_scan()
        payloads = sent(ha)
    assert [p["type"] for p in payloads] == ["preorder"]
    assert "Die Hard" in payloads[0]["message"]
    assert payloads[0]["url"].startswith("https://www.blu-ray.com/link/click.php")

    # Running again does not repeat the alert.
    with tracker.db.conn() as c:
        c.execute("UPDATE releases SET last_checked='2000-01-01'")
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, read("product_preorder.html").replace("Braveheart", "Die Hard"))
        await tracker.run_scan()
        assert sent(ha) == []


@pytest.mark.asyncio
async def test_new_announcement_goes_to_digest(tracker):
    tracker.db.set_meta("seeded", "x")  # skip seeding: everything found is "new"
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, read("product_announced.html"))
        await tracker.run_scan()
        assert sent(ha) == []
        await tracker.send_digest()
        payloads = sent(ha)
    assert len(payloads) == 1 and payloads[0]["type"] == "digest"
    assert payloads[0]["count"] == 2
    assert "Interstellar in stock (Walmart)" in payloads[0]["message"]
    assert tracker.db.pending_digest() == []


@pytest.mark.asyncio
async def test_purchased_and_hidden_suppress_alerts(tracker):
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, read("product_announced.html"))
        await tracker.run_scan()
    tracker.db.set_flag(422773, "purchased", True)
    with tracker.db.conn() as c:
        c.execute("UPDATE releases SET last_checked=NULL, preorder=1 WHERE id=422773")
    assert tracker.db.pending_preorder_alerts() == []


@pytest.mark.asyncio
async def test_blocked_source_alerts_once(tracker):
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.blu-ray.com/").respond(200, text="home")
        router.get(url__startswith="https://www.blu-ray.com/movies/search.php").respond(200, text="error42")
        router.get("https://www.reddit.com/r/steelbooks/new/.rss").respond(200, text=read("reddit.xml"))
        ha = router.post(HA).respond(200)
        import app.bluray as br
        orig = br.asyncio.sleep

        async def fast_sleep(_):
            return await orig(0)
        br.asyncio.sleep = fast_sleep
        try:
            for _ in range(4):
                await tracker.run_scan()
        finally:
            br.asyncio.sleep = orig
        health = [p for p in sent(ha) if p["type"] == "health"]
    assert len(health) == 1
    assert "Blu-ray.com search" in health[0]["message"]
