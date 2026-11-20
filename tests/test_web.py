import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import shops

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def client():
    return TestClient(main.app)  # no lifespan: the scheduler doesn't start


def add(rid: int, title: str, release_date: str | None, **fields) -> None:
    main.db.insert_release({"id": rid, "title": title, "edition": None, "studio": None, "year": None,
                            "release_date": release_date, "url": f"https://www.blu-ray.com/movies/x/{rid}/"},
                           seeding=True)
    with main.db.conn() as c:
        for key, value in fields.items():
            c.execute(f"UPDATE releases SET {key}=? WHERE id=?", (value, rid))


def test_icon_and_favicon_are_served(client):
    for path in ("/static/icon.png", "/favicon.ico"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    page = client.get("/")
    assert page.status_code == 200
    assert 'rel="icon"' in page.text


def test_released_title_is_not_shown_as_a_live_preorder(client):
    # "Today" is pinned to 2026-10-11 by the frozen_today fixture.
    add(9001, "Already Out 4K", "2026-10-01", is_steelbook=1, preorder=1, price="$29.99",
        availability="Available for pre-order")
    add(9002, "Still To Come 4K", "2026-12-01", is_steelbook=1, preorder=1, price="$31.27",
        availability="Available for pre-order")

    summary = client.get("/api/summary").json()
    assert summary["preorder"] == 1
    assert summary["next_title"] == "Still To Come 4K"
    assert summary["scanning"] is False

    preorders = client.get("/api/releases", params={"view": "preorder"}).json()
    assert [r["id"] for r in preorders] == [9002]

    page = client.get("/", params={"view": "all"}).text
    out, upcoming = page.split("Already Out 4K")[1].split("Still To Come 4K")
    assert "Released" in out and "Preorder live" not in out
    assert "Preorder live" in upcoming


def test_purchased_and_hidden_flags(client):
    add(9003, "Flag Me 4K", "2026-12-01", is_steelbook=1)
    assert client.post("/api/releases/9003/purchased", json={"value": True}).json() == {"id": 9003, "purchased": True}
    assert [r["id"] for r in client.get("/api/releases", params={"view": "purchased"}).json()] == [9003]
    assert 9003 not in [r["id"] for r in client.get("/api/releases").json()]
    client.post("/api/releases/9003/purchased", json={"value": False})
    assert 9003 in [r["id"] for r in client.get("/api/releases").json()]

    assert client.post("/api/releases/9003/deleted", json={"value": True}).status_code == 404
    assert client.post("/api/releases/123456789/hidden", json={"value": True}).status_code == 404


def test_digest_time_that_cannot_be_read_falls_back_to_nine():
    now = datetime(2026, 10, 11, 8, 0)
    assert main._seconds_until("09:30", now) == 90 * 60
    assert main._seconds_until("07:00", now) == 23 * 3600  # already past today
    for bad in ("", "9", "25:00", "09:75", "nine"):
        assert main._seconds_until(bad, now) == 3600, bad


def card(page: str, title: str) -> str:
    """One release's card, cut out of the page."""
    start = page.index(f">{title}</a>")
    end = page.find('<div class="card', start)
    return page[start:end if end > 0 else page.index("</main>")]


def test_cards_show_the_store_of_an_exclusive(client):
    add(9010, "Street Fighter 4K", None, is_steelbook=1, preorder=1, exclusive="Amazon",
        availability="Available for pre-order")
    add(9011, "Gone with the Wind 4K", "2026-12-01", is_steelbook=1, exclusive="Walmart")
    add(9012, "Resident Evil 4K", "2026-12-29", is_steelbook=1, unavailable=1, availability="Currently unavailable")

    page = client.get("/").text
    amazon = card(page, "Street Fighter 4K")
    assert "Amazon exclusive" in amazon and "Preorder live" in amazon
    assert "can't alert you" not in amazon
    # Blu-ray.com has no stock for a Walmart exclusive, and the card says what that means.
    walmart = card(page, "Gone with the Wind 4K")
    assert "Walmart exclusive" in walmart
    assert "Sold only at Walmart" in walmart and "can't alert you" in walmart
    plain = card(page, "Resident Evil 4K")
    assert "exclusive" not in plain
    assert 'class="badge out">Currently unavailable' in plain

    by_id = {r["id"]: r for r in client.get("/api/releases").json()}
    assert by_id[9010]["exclusive"] == "Amazon"
    assert by_id[9012]["exclusive"] is None and by_id[9012]["unavailable"] == 1


def test_page_lists_sources_and_no_reddit_posts(client):
    page = client.get("/").text
    assert "<h2>Sources</h2>" in page
    assert "r/steelbooks" not in page


def test_page_shows_what_can_be_ordered_at_the_shops(client):
    gruv = next(s for s in shops.SHOPS if s.key == "gruv")
    feed = json.loads((FIX / "shop_gruv.json").read_text(encoding="utf-8"))
    main.db.apply_shop_items("gruv", shops.parse_products(gruv, feed["products"]), quiet=True, realert_before="")

    page = client.get("/").text
    panel = page.split("<h2>At the shops</h2>")[1].split("<h2>")[0]
    assert ('<a href="https://www.gruv.com/products/prisoners-of-the-ghostland-4k-ultra-hd-steelbook-uhd-_1000866575"'
            ' target="_blank" rel="noopener">Prisoners Of The Ghostland (4K Ultra HD Steelbook)</a>') in panel
    assert "Gruv · AMC · $27.57 · In stock" in panel
    assert "Gruv · Shout! Factory · $39.99 · In stock" in panel
    assert "House of the Dragon" not in panel  # sold out
    assert '<a href="https://www.gruv.com/collections/4k-steelbooks" target="_blank" rel="noopener">Gruv</a>: 2 of 3 can be ordered' in panel
    # The other shops are named even before their first read.
    assert ">Lionsgate Limited</a>" in panel and ">MVD Shop</a>" in panel


def test_page_shows_the_latest_headlines(client):
    main.db.insert_headline({"id": "p1", "title": "The Odyssey 4k SteelBooks Are Sold Out", "published": None,
                             "url": "https://hd-report.com/2026/11/12/the-odyssey/"}, digest_sent=True)
    panel = client.get("/").text.split("<h2>Steelbook news</h2>")[1].split("</section>")[0]
    assert '<a href="https://hd-report.com/2026/11/12/the-odyssey/" target="_blank" rel="noopener">' \
           'The Odyssey 4k SteelBooks Are Sold Out</a>' in panel
    assert "HD Report" in panel


def test_exclusive_at_a_watched_shop_says_its_alerts_come_from_the_shop(client):
    add(9020, "Jackie Brown 4K", "2026-12-15", is_steelbook=1, exclusive="Lionsgate Limited")
    add(9021, "Clayface 4K", "2026-12-15", is_steelbook=1, exclusive="Target")

    page = client.get("/").text
    watched = card(page, "Jackie Brown 4K")
    assert "Sold only at Lionsgate Limited." in watched and "alerts you from there" in watched
    assert "can't alert you" not in watched
    assert "can't alert you" in card(page, "Clayface 4K")


async def test_scan_now_also_reads_the_shops(monkeypatch):
    calls = []

    async def scan():
        calls.append("scan")

    async def read_shops():
        calls.append("shops")

    monkeypatch.setattr(main.tracker, "run_scan", scan)
    monkeypatch.setattr(main.tracker, "run_shops", read_shops)
    await main._scan_everything()
    assert sorted(calls) == ["scan", "shops"]


def test_summary_says_when_the_shops_were_last_read(client):
    assert client.get("/api/summary").json()["last_shop_run"] is None
