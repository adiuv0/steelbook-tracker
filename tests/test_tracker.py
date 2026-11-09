"""End-to-end scans against mocked Blu-ray.com, Reddit and Home Assistant."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from app import config
from app.db import DB
from app.jobs import Tracker

from .conftest import TODAY

FIX = Path(__file__).parent / "fixtures"
HA = "http://ha.local:8123/api/webhook/steelbooks"
HOME = "https://www.blu-ray.com/"
SEARCH = "https://www.blu-ray.com/movies/search.php"
REDDIT = "https://www.reddit.com/r/steelbooks/new/.rss"
DIE_HARD = "https://www.blu-ray.com/movies/Die-Hard-4K-Blu-ray/422773/"
OTHER_MOMMY = "https://www.blu-ray.com/movies/Other-Mommy-4K-Blu-ray/422840/"
BURBS = "https://www.blu-ray.com/movies/The-Burbs-4K-Blu-ray/421959/"
ANY_PRODUCT = r"https://www\.blu-ray\.com/movies/.+/\d+/$"
PRODUCT_HEALTH = "Blu-ray.com product pages"
HA_HEALTH = "Home Assistant webhook"


def read(name: str) -> str:
    return (FIX / name).read_text()


DIE_HARD_ON_PREORDER = read("product_preorder.html").replace("Braveheart", "Die Hard")


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = replace(config.load(), ha_webhook_url=HA, request_delay_min=0, request_delay_max=0,
                seed_pages=1, scan_pages=1, failure_backoff_seconds=0)
    return Tracker(s, DB(s.db_path))


def mock_sites(router, die_hard_page: str, preorder_listing: str | None = None):
    """Blu-ray.com, Reddit and Home Assistant. Both search listings return the same
    fixture page unless ``preorder_listing`` gives the newest-preorders one its own."""
    router.get(HOME).respond(200, text="<html>home</html>")
    if preorder_listing is not None:
        router.get(SEARCH, params__contains={"sortby": "preordertimestamp"}).respond(200, text=preorder_listing)
    router.get(url__startswith=SEARCH).respond(200, text=read("search_simple.html"))
    router.get(DIE_HARD).respond(200, text=die_hard_page)
    router.get(OTHER_MOMMY).respond(200, text=read("product_announced.html").replace("Die Hard", "Other Mommy"))
    router.get(BURBS).respond(200, text=read("product_not_steelbook.html"))
    router.get(REDDIT).respond(200, text=read("reddit.xml"))
    return router.post(HA).respond(200)


def listing(*rows: tuple[int, str, str, str]) -> str:
    """A search results page: (product id, url slug, title, release date) per row."""
    body = "".join(
        f'<tr><td width="3%"></td><td width="52%">'
        f'<a href="https://www.blu-ray.com/movies/{slug}/{pid}/">{title}</a></td>'
        f'<td width="25%">Some Studio</td><td width="20%">{when}</td></tr>'
        for pid, slug, title, when in rows)
    return f"<html><body><table>{body}</table></body></html>"


def sent(ha_route) -> list[dict]:
    return [json.loads(c.request.content) for c in ha_route.calls]


def fetched(router, url: str) -> int:
    return sum(1 for c in router.calls if str(c.request.url) == url)


def hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def set_last_checked(tracker, when: str, rid: int | None = None) -> None:
    with tracker.db.conn() as c:
        if rid is None:
            c.execute("UPDATE releases SET last_checked=?", (when,))
        else:
            c.execute("UPDATE releases SET last_checked=? WHERE id=?", (when, rid))


def fail_count(tracker, source: str) -> int | None:
    return next((h["fail_count"] for h in tracker.db.health() if h["name"] == source), None)


def ids(rows) -> set[int]:
    return {r["id"] for r in rows}


def add_release(tracker, rid: int, **fields) -> None:
    tracker.db.insert_release(
        {"id": rid, "title": f"Movie {rid} 4K", "edition": None, "studio": None, "year": None,
         "release_date": "2026-12-01", "url": f"https://www.blu-ray.com/movies/Movie-4K-Blu-ray/{rid}/"},
        seeding=True)
    with tracker.db.conn() as c:
        for key, value in fields.items():
            c.execute(f"UPDATE releases SET {key}=? WHERE id=?", (value, rid))


async def test_seed_then_preorder_alert(tracker):
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, read("product_announced.html"))
        result = await tracker.run_scan()
        assert result.startswith("Seeded"), result
        # Seeding sends one setup message, never a flood of alerts.
        assert [p["type"] for p in sent(ha)] == ["setup"]

    # Old '80s Horror' release (Oct 6) is in the past and skipped; Burbs is not a steelbook.
    assert ids(tracker.db.list_releases("all", TODAY.isoformat())) == {422840, 422773}

    # Force a recheck of Die Hard now that its preorder opened.
    set_last_checked(tracker, "2000-01-01", 422773)
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, DIE_HARD_ON_PREORDER)
        await tracker.run_scan()
        payloads = sent(ha)
    assert [p["type"] for p in payloads] == ["preorder"]
    assert "Die Hard" in payloads[0]["message"]
    assert payloads[0]["url"].startswith("https://www.blu-ray.com/link/click.php")

    # Running again does not repeat the alert.
    set_last_checked(tracker, "2000-01-01")
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, DIE_HARD_ON_PREORDER)
        await tracker.run_scan()
        assert sent(ha) == []


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


async def test_purchased_and_hidden_suppress_alerts(tracker):
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, read("product_announced.html"))
        await tracker.run_scan()
    tracker.db.set_flag(422773, "purchased", True)
    with tracker.db.conn() as c:
        c.execute("UPDATE releases SET last_checked=NULL, preorder=1 WHERE id=422773")
    assert tracker.db.pending_preorder_alerts() == []


async def test_blocked_source_alerts_once(tracker, no_waiting):
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        router.get(HOME).respond(200, text="home")
        router.get(url__startswith=SEARCH).respond(200, text="error42")
        router.get(REDDIT).respond(200, text=read("reddit.xml"))
        ha = router.post(HA).respond(200)
        for _ in range(4):
            await tracker.run_scan()
        health = [p for p in sent(ha) if p["type"] == "health"]
    assert len(health) == 1
    assert "Blu-ray.com search" in health[0]["message"]


async def test_unreachable_site_is_reported_and_the_rest_of_the_run_still_happens(tracker):
    """Blu-ray.com not answering at all is the likeliest way for it to break."""
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        router.get(HOME).mock(side_effect=httpx.ConnectError("All connection attempts failed"))
        router.get(REDDIT).respond(200, text=read("reddit.xml"))
        ha = router.post(HA).respond(200)
        for _ in range(4):
            result = await tracker.run_scan()
        health = [p for p in sent(ha) if p["type"] == "health"]
    assert result.startswith("Scan done"), result
    assert len(health) == 1
    assert "Blu-ray.com search" in health[0]["message"]
    assert "ConnectError" in health[0]["message"]
    assert len(tracker.db.pending_community()) == 2  # Reddit was still read


async def test_first_scan_is_not_marked_done_when_the_listings_cannot_be_read(tracker, no_waiting):
    """Otherwise the next scan would announce every existing steelbook as new."""
    with respx.mock(assert_all_called=False) as router:
        router.get(HOME).respond(200, text="home")
        router.get(url__startswith=SEARCH).respond(200, text="error42")
        router.get(REDDIT).respond(200, text=read("reddit.xml"))
        ha = router.post(HA).respond(200)
        result = await tracker.run_scan()
        assert result.startswith("First scan could not"), result
        assert sent(ha) == []
    assert tracker.db.get_meta("seeded") is None
    assert tracker.has_backlog()  # so it is retried soon, not in four hours

    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, DIE_HARD_ON_PREORDER)
        result = await tracker.run_scan()
        assert result.startswith("Seeded"), result
        assert [p["type"] for p in sent(ha)] == ["setup"]
    assert not tracker.has_backlog()


async def test_connection_blip_does_not_end_the_batch(tracker):
    """One failed product page pauses and carries on; it doesn't drop the rest."""
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, read("product_announced.html"))
        # Newest listing is checked first and fails once with a connection error.
        router.get(OTHER_MOMMY).mock(
            side_effect=[httpx.ConnectError("All connection attempts failed"),
                         httpx.Response(200, text=read("product_announced.html"))])
        await tracker.run_scan()
    with tracker.db.conn() as c:
        checked = ids(c.execute("SELECT id FROM releases WHERE last_checked IS NOT NULL"))
        unchecked = ids(c.execute("SELECT id FROM releases WHERE last_checked IS NULL"))
    assert checked == {422773, 421959}
    assert unchecked == {422840}
    assert tracker.has_backlog()
    assert fail_count(tracker, PRODUCT_HEALTH) == 0


async def test_unchecked_listings_are_visible_and_checked_newest_first(tracker):
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, read("product_announced.html"))
        for url in (DIE_HARD, OTHER_MOMMY, BURBS):
            router.get(url).mock(side_effect=httpx.ConnectError("All connection attempts failed"))
        await tracker.run_scan()
        product_calls = [str(c.request.url) for c in router.calls if "/movies/" in str(c.request.url)
                         and "search.php" not in str(c.request.url)]
    # Highest product id (newest listing) is tried first.
    assert product_calls[0].endswith("/422840/")
    rows = tracker.db.list_releases("upcoming", TODAY.isoformat())
    assert ids(rows) == {422840, 422773, 421959}
    assert all(r["is_steelbook"] is None for r in rows)
    assert tracker.db.counts(TODAY.isoformat())["unverified"] == 3


async def test_follow_up_scans_back_off_while_nothing_gets_through(tracker):
    tracker.db.set_meta("seeded", "x")
    regular = tracker.s.poll_hours * 3600
    assert abs(tracker.next_scan_delay() - regular) <= 600  # nothing waiting: the normal interval

    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, read("product_announced.html"))
        for url in (DIE_HARD, OTHER_MOMMY, BURBS):
            router.get(url).mock(side_effect=httpx.ConnectError("All connection attempts failed"))
        await tracker.run_scan()
    assert tracker.has_backlog()
    assert tracker.stalled == 1
    assert tracker.next_scan_delay() == 40 * 60
    tracker.stalled = 2
    assert tracker.next_scan_delay() == 80 * 60
    tracker.stalled = 500
    assert tracker.next_scan_delay() == regular  # never slower than the normal interval
    tracker.stalled = 0
    assert tracker.next_scan_delay() == 20 * 60  # the first follow-up comes quickly

    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, read("product_announced.html"))
        await tracker.run_scan()
    assert tracker.stalled == 0
    assert not tracker.has_backlog()


async def test_product_pages_all_failing_is_not_recorded_as_healthy(tracker):
    """Also when fewer than three pages were due, which is the normal case."""
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, read("product_announced.html"))
        await tracker.run_scan()
    set_last_checked(tracker, hours_ago(13), 422773)  # only Die Hard is due again
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, read("product_announced.html"))
        router.get(DIE_HARD).mock(side_effect=httpx.ReadTimeout(""))
        await tracker.run_scan()
        assert fail_count(tracker, PRODUCT_HEALTH) == 1
        await tracker.run_scan()
        await tracker.run_scan()
        health = [p for p in sent(ha) if p["type"] == "health"]
    assert len(health) == 1
    assert PRODUCT_HEALTH in health[0]["message"]
    assert "ReadTimeout" in health[0]["message"]  # timeouts have no message of their own


async def test_block_page_does_not_drop_a_listing(tracker):
    """A page that isn't a product page is a failed check, not 'not a steelbook'."""
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, "<html><head><title>Robot check</title></head><body>Are you human?</body></html>")
        await tracker.run_scan()
    row = tracker.db.get_release(422773)
    assert row["is_steelbook"] is None and row["last_checked"] is None
    assert 422773 in ids(tracker.db.list_releases("upcoming", TODAY.isoformat()))


async def test_deleted_listing_is_dropped(tracker):
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, "<html><head></head><body>No such movie: 422773</body></html>")
        await tracker.run_scan()
    assert tracker.db.get_release(422773)["is_steelbook"] == 0
    assert 422773 not in ids(tracker.db.list_releases("all", TODAY.isoformat()))


async def test_accented_titles_are_stored_correctly(tracker):
    """Blu-ray.com pages are Windows-1252 and are served without a charset header."""
    page = listing((500001, "Amelie-4K-Blu-ray", "Am\xe9lie 4K (Limited Collector\x92s Edition)", "Dec 01, 2026"))
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, read("product_announced.html"))
        router.get(url__startswith=SEARCH).respond(
            200, content=page.encode("latin-1"), headers={"Content-Type": "text/html"})
        router.get(url__regex=ANY_PRODUCT).respond(200, text=read("product_announced.html"))
        await tracker.run_scan()
    row = tracker.db.get_release(500001)
    assert row["title"] == "Amélie 4K"
    assert row["edition"] == "Limited Collector’s Edition"


async def test_preorders_are_kept_current_until_release(tracker):
    """A title on preorder is still rechecked (daily), so a sold-out preorder, a new
    price or a release date that only appears later all show up."""
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, DIE_HARD_ON_PREORDER)
        await tracker.run_scan()
    assert tracker.db.get_release(422773)["preorder"] == 1

    set_last_checked(tracker, hours_ago(13), 422773)  # under a day: not due yet
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, read("product_unavailable.html"))
        await tracker.run_scan()
        assert fetched(router, DIE_HARD) == 0

    set_last_checked(tracker, hours_ago(25), 422773)
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, read("product_unavailable.html"))
        await tracker.run_scan()
        assert fetched(router, DIE_HARD) == 1
        assert sent(ha) == []  # selling out is not an alert
    row = tracker.db.get_release(422773)
    assert row["preorder"] == 0
    assert row["availability"] == "Currently unavailable"
    assert row["price"] == "$47.99"


async def test_released_titles_are_no_longer_rechecked(tracker):
    tracker.db.set_meta("seeded", "x")
    add_release(tracker, 1, release_date="2026-10-01", is_steelbook=1, preorder=1, last_checked=hours_ago(500))
    add_release(tracker, 2, release_date="2026-10-01", is_steelbook=1, preorder=0, last_checked=hours_ago(500))
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, read("product_announced.html"))
        await tracker.run_scan()
        assert not any("/Movie-4K-Blu-ray/" in str(c.request.url) for c in router.calls)


def test_released_titles_are_not_listed_as_preorders(tracker):
    """The preorder flag is frozen at release, so the Preorder view must go by date too."""
    add_release(tracker, 1, release_date="2026-10-01", is_steelbook=1, preorder=1)  # out already
    add_release(tracker, 2, release_date="2026-12-01", is_steelbook=1, preorder=1)
    add_release(tracker, 3, release_date=None, is_steelbook=1, preorder=1)  # date not announced
    today = TODAY.isoformat()
    assert ids(tracker.db.list_releases("preorder", today)) == {2, 3}
    assert tracker.db.counts(today)["preorder"] == 2
    assert ids(tracker.db.list_releases("all", today)) == {1, 2, 3}


async def test_newest_preorders_listing_gets_a_known_title_checked_right_away(tracker):
    """A known title turning up in Blu-ray.com's newest-preorders list means its
    preorder just opened: check it this scan instead of at its next 12-hourly recheck."""
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, read("product_announced.html"))
        await tracker.run_scan()
    assert tracker.db.get_release(422773)["preorder"] == 0

    # Three hours on, nothing is due a routine recheck. Die Hard is not in the list yet.
    set_last_checked(tracker, hours_ago(3))
    other_mommy_only = listing((422840, "Other-Mommy-4K-Blu-ray", "Other Mommy 4K", "-"))
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, DIE_HARD_ON_PREORDER, preorder_listing=other_mommy_only)
        await tracker.run_scan()
        assert fetched(router, DIE_HARD) == 0
        # Other Mommy is listed there but its page says otherwise: looked at once...
        assert fetched(router, OTHER_MOMMY) == 1
        await tracker.run_scan()
        # ...and not again by a scan that follows straight after.
        assert fetched(router, OTHER_MOMMY) == 1

    die_hard_listed = listing((422773, "Die-Hard-4K-Blu-ray", "Die Hard 4K", "Dec 01, 2026"))
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, DIE_HARD_ON_PREORDER, preorder_listing=die_hard_listed)
        await tracker.run_scan()
        assert fetched(router, DIE_HARD) == 1
        assert [p["type"] for p in sent(ha)] == ["preorder"]


async def test_webhook_failure_is_shown_and_the_alert_is_retried(tracker):
    tracker.db.set_meta("seeded", "x")
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, DIE_HARD_ON_PREORDER)
        router.post(HA).respond(500)
        result = await tracker.run_scan()
    assert "not delivered" in result
    assert ids(tracker.db.pending_preorder_alerts()) == {422773}
    assert fail_count(tracker, HA_HEALTH) == 1  # listed under Sources in the web UI

    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, DIE_HARD_ON_PREORDER)
        await tracker.run_scan()
        assert [p["type"] for p in sent(ha)] == ["preorder"]
    assert tracker.db.pending_preorder_alerts() == []
    assert fail_count(tracker, HA_HEALTH) == 0


async def test_mistyped_webhook_url_does_not_break_the_scan(tracker):
    tracker.db.set_meta("seeded", "x")
    tracker.s = replace(tracker.s, ha_webhook_url="http://192.168.1.10:8123:8123/api/webhook/x")
    with respx.mock(assert_all_called=False) as router:
        mock_sites(router, DIE_HARD_ON_PREORDER)
        result = await tracker.run_scan()
    assert result.startswith("Scan done"), result
    assert fail_count(tracker, HA_HEALTH) == 1
    assert ids(tracker.db.pending_preorder_alerts()) == {422773}


async def test_no_webhook_means_log_only(tracker):
    """Without a webhook, alerts are logged and marked done rather than piling up."""
    tracker.db.set_meta("seeded", "x")
    tracker.s = replace(tracker.s, ha_webhook_url="")
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, DIE_HARD_ON_PREORDER)
        result = await tracker.run_scan()
        assert "HA_WEBHOOK_URL is not set" in await tracker.send_digest(force=True)
        assert sent(ha) == []
    assert "1 preorder alerts (not sent: HA_WEBHOOK_URL is not set)" in result
    assert tracker.db.pending_preorder_alerts() == []
    assert fail_count(tracker, HA_HEALTH) is None


def test_seeded_listing_already_on_preorder_stays_quiet(tracker):
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
