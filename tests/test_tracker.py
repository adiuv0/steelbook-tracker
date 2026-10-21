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
                seed_pages=1, scan_pages=1, failure_backoff_seconds=0)
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


@pytest.mark.asyncio
async def test_connection_blip_does_not_end_the_batch(tracker):
    """One failed product page pauses and carries on; it doesn't drop the rest."""
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, read("product_announced.html"))
        # Newest listing is checked first and fails once with a connection error.
        router.get("https://www.blu-ray.com/movies/Other-Mommy-4K-Blu-ray/422840/").mock(
            side_effect=[httpx.ConnectError("All connection attempts failed"),
                         httpx.Response(200, text=read("product_announced.html"))])
        await tracker.run_scan()
    with tracker.db.conn() as c:
        checked = {r["id"] for r in c.execute("SELECT id FROM releases WHERE last_checked IS NOT NULL")}
        unchecked = {r["id"] for r in c.execute("SELECT id FROM releases WHERE last_checked IS NULL")}
    assert checked == {422773, 421959}
    assert unchecked == {422840}
    assert tracker.has_backlog()
    assert [h["fail_count"] for h in tracker.db.health() if h["name"] == "Blu-ray.com product pages"] == [0]


@pytest.mark.asyncio
async def test_unchecked_listings_are_visible_and_checked_newest_first(tracker):
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.blu-ray.com/").respond(200, text="home")
        router.get(url__startswith="https://www.blu-ray.com/movies/search.php").respond(
            200, text=read("search_simple.html"))
        router.get(url__regex=r"https://www\.blu-ray\.com/movies/.+/\d+/$").mock(
            side_effect=httpx.ConnectError("All connection attempts failed"))
        router.get("https://www.reddit.com/r/steelbooks/new/.rss").respond(200, text=read("reddit.xml"))
        router.post(HA).respond(200)
        await tracker.run_scan()
        product_calls = [str(c.request.url) for c in router.calls if "/movies/" in str(c.request.url)
                         and "search.php" not in str(c.request.url)]
    # Highest product id (newest listing) is tried first.
    assert product_calls[0].endswith("/422840/")
    rows = tracker.db.list_releases("upcoming", "2026-10-11")
    assert {r["id"] for r in rows} == {422840, 422773, 421959}
    assert all(r["is_steelbook"] is None for r in rows)
    assert tracker.db.counts("2026-10-11")["unverified"] == 3


@pytest.mark.asyncio
async def test_seeded_listing_already_on_preorder_stays_quiet(tracker):
    """A listing found during the quiet first scan but checked later doesn't alert
    for a preorder that was already open; a genuinely new listing does."""
    tracker.db.insert_release({"id": 1, "title": "Old 4K", "edition": None, "studio": None, "year": None,
                               "release_date": "2026-12-01", "url": "https://x/1/"}, seeding=True)
    tracker.db.insert_release({"id": 2, "title": "New 4K", "edition": None, "studio": None, "year": None,
                               "release_date": "2026-12-01", "url": "https://x/2/"}, seeding=False)
    product = {"is_steelbook": 1, "preorder": 1, "price": "$30.00", "availability": "Available for pre-order",
               "buy_url": None, "cover": None, "release_date": None}
    tracker.db.apply_product(1, product)
    tracker.db.apply_product(2, product)
    assert [r["id"] for r in tracker.db.pending_preorder_alerts()] == [2]
    assert [r["id"] for r in tracker.db.pending_digest()] == [2]
