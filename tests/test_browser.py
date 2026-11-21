"""SHOP_READER=browser: the shops read with a real Chromium.

The first half runs the real browser against a stand-in shop on localhost
(tests/shopfront.py) and says what the browser may and may not do there. The second
half is the tracker's side of it, most of it without a browser.
"""

import asyncio
import json
import os
import re
import socket
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import respx

from app import browser, config, jobs, shops
from app.db import DB
from app.jobs import Tracker
from tests.shopfront import Shopfront
from tests.test_shops import (CLAYFACE, FEEDS, HA, NOON, SHOP, clock, fail_count, fails,  # noqa: F401
                              last_error, plus, waits)

ROOT = Path(__file__).parent.parent
PAGE = "/collections/steelbooks"
LIST = "/collections/steelbooks/products.json"
GRUV = FEEDS["gruv"]["products"]  # three products, all 4K steelbooks


@pytest.fixture
def front():
    with Shopfront({LIST: GRUV}) as f:
        yield f


def shop_at(site: str) -> shops.Shop:
    return shops.Shop("standin", "Stand-in Shop", site, LIST, PAGE, vendor_is_studio=True)


async def read(front, profile, chromium) -> list[dict]:
    """One read of the stand-in shop: the browser is started, used and closed."""
    async with browser.BrowserReader(profile, chromium) as reader:
        return await shops.fetch(reader, shop_at(front.url))


# --- what the browser does at a shop ------------------------------------------

async def test_the_shops_page_is_opened_whole_and_the_page_asks_for_the_list(front, tmp_path, chromium):
    items = await read(front, tmp_path / "profile", chromium)
    assert items == shops.parse_products(shop_at(front.url), GRUV)
    assert len(items) == 3

    paths = front.paths()
    assert paths[0] == PAGE  # the page a person would browse comes first
    # ...with what is on it: its stylesheet, its image, and its script has run.
    assert {"/theme.css", "/logo.png", "/seen"} <= set(paths)
    assert paths.count(PAGE) == 1 and paths.count(LIST) == 1
    page, listing = front.last(PAGE), front.last(LIST)
    assert (page.headers["sec-fetch-mode"], page.headers["sec-fetch-dest"]) == ("navigate", "document")
    # The list is asked for by the shop's own page, as its scripts would ask.
    assert listing.headers["referer"] == front.url + PAGE
    assert listing.headers["sec-fetch-site"] == "same-origin"
    assert listing.query == {"limit": ["250"], "page": ["1"]}
    assert listing.headers["user-agent"] == page.headers["user-agent"]


async def test_cookies_are_kept_from_one_visit_to_the_next(front, tmp_path, chromium):
    """Like a person's own browser: the shop sees one returning visitor, not a new one every hour."""
    await read(front, tmp_path / "profile", chromium)
    assert "cookie" not in front.seen[0].headers  # a first visit
    await read(front, tmp_path / "profile", chromium)  # the browser was closed in between
    assert front.last(PAGE).headers["cookie"] == "visitor=1"
    assert front.visitors == 1


def running_on(profile: Path) -> list[str]:
    """Command lines of the processes that have this browser profile open."""
    found = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                line = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if str(profile) in line:
                found.append(line)
    return found


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc to look for the browser")
async def test_the_browser_only_runs_during_a_read(front, tmp_path, chromium):
    profile = tmp_path / "profile"
    async with browser.BrowserReader(profile, chromium) as reader:
        await shops.fetch(reader, shop_at(front.url))
        assert running_on(profile)
    for _ in range(50):  # its last helper processes may take a moment to go
        if not running_on(profile):
            break
        await asyncio.sleep(0.1)
    assert running_on(profile) == []


async def test_the_browser_does_not_hide_what_it_is(front, tmp_path, chromium):
    """No disguise: a shop that asks can tell a program is looking. If this test starts
    failing, something has been added that the top of app/browser.py rules out."""
    await read(front, tmp_path / "profile", chromium)
    assert "HeadlessChrome" in front.last(PAGE).headers["user-agent"]
    assert front.last("/seen").query == {"webdriver": ["true"]}  # what the page's own script saw


def test_nothing_that_disguises_a_browser_is_installed():
    wanted = "".join((ROOT / name).read_text() for name in ("requirements.txt", "requirements-dev.txt"))
    assert not re.search(r"stealth|undetected|fingerprint|captcha|proxy", wanted, re.IGNORECASE)


async def test_the_browser_is_not_started_until_a_shop_is_asked(tmp_path, chromium):
    async with browser.BrowserReader(tmp_path / "profile", chromium):
        pass  # every shop was being left alone, say
    assert not (tmp_path / "profile").exists()


# --- when the shop says no ----------------------------------------------------

async def test_too_many_requests_on_the_page_ends_the_read(front, tmp_path, chromium):
    front.answers[PAGE] = (429, {"Retry-After": "120"}, "Too many requests")
    with pytest.raises(shops.Throttled) as refusal:
        await read(front, tmp_path / "profile", chromium)
    assert type(refusal.value) is shops.Throttled and refusal.value.wait == 120
    assert front.paths().count(PAGE) == 1  # asked once, not again
    assert LIST not in front.paths()


async def test_too_many_requests_on_the_list_ends_the_read(front, tmp_path, chromium):
    front.answers[LIST] = (429, {"Retry-After": "60"}, '{"errors": "Too many requests"}')
    with pytest.raises(shops.Throttled) as refusal:
        await read(front, tmp_path / "profile", chromium)
    assert type(refusal.value) is shops.Throttled and refusal.value.wait == 60
    assert front.paths().count(LIST) == 1


@pytest.mark.parametrize("where, answer, said", [
    # Shopify sends a visitor it doubts to /challenge, a page with a captcha.
    (PAGE, (302, {"Location": "/challenge"}, ""), "asked for a check that only a person can pass"),
    (LIST, (302, {"Location": "/challenge"}, ""), "asked for a check that only a person can pass"),
    # Cloudflare's "Just a moment..." page.
    (PAGE, (403, {"cf-mitigated": "challenge"}, "<title>Just a moment...</title>"),
     "asked for a check that only a person can pass"),
    (PAGE, (403, {}, "Forbidden"), "would not serve the browser"),
    (LIST, (403, {}, "Forbidden"), "would not serve the browser"),
])
async def test_a_check_for_people_is_never_answered(front, tmp_path, chromium, where, answer, said):
    front.answers[where] = answer
    with pytest.raises(shops.Challenged) as refusal:
        await read(front, tmp_path / "profile", chromium)
    assert refusal.value.said == said and str(refusal.value) == f"the shop {said}"
    assert front.paths().count(where) == 1  # not tried a second time
    if where == PAGE:
        assert LIST not in front.paths()  # and the list is not asked for behind the check's back
    # Whatever the check's page offers, nothing is sent to it.
    assert [s.method for s in front.seen if s.path == "/challenge"] in ([], ["GET"])


async def test_other_errors_are_failures_not_refusals(front, tmp_path, chromium):
    front.answers[PAGE] = (500, {}, "Something went wrong")
    with pytest.raises(RuntimeError, match=r"the shop's page answered with an error \(500\)"):
        await read(front, tmp_path / "profile", chromium)
    assert LIST not in front.paths()

    del front.answers[PAGE]
    front.answers[LIST] = (200, {}, "<html>not a list</html>")
    with pytest.raises(RuntimeError, match="the shop's product list is not in the expected format"):
        await read(front, tmp_path / "profile", chromium)

    front.answers[LIST] = (404, {}, "Not found")
    with pytest.raises(RuntimeError, match=r"the shop's product list answered with an error \(404\)"):
        await read(front, tmp_path / "profile", chromium)


async def test_shop_that_cannot_be_reached_is_reported_in_one_line(tmp_path, chromium):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{s.getsockname()[1]}"  # nothing listens there once this is closed
    async with browser.BrowserReader(tmp_path / "profile", chromium) as reader:
        with pytest.raises(RuntimeError) as failure:
            await shops.fetch(reader, shop_at(closed))
    message = str(failure.value)
    assert message.startswith("the browser could not open the shop's page: ")
    assert "ERR_CONNECTION_REFUSED" in message and "\n" not in message


async def test_browser_that_cannot_be_started_is_reported(tmp_path):
    async with browser.BrowserReader(tmp_path / "profile", str(tmp_path / "no-such-chromium")) as reader:
        with pytest.raises(RuntimeError, match="the browser could not be started: "):
            await shops.fetch(reader, shop_at("http://127.0.0.1:9"))


# --- the rest of a read -------------------------------------------------------

async def test_long_list_is_read_page_by_page_from_the_one_open_page(front, tmp_path, chromium, monkeypatch):
    monkeypatch.setattr(shops, "PAGE_SIZE", 2)
    monkeypatch.setattr(shops, "PAGE_GAP", 0)
    assert len(await read(front, tmp_path / "profile", chromium)) == 3
    assert [s.query["page"] for s in front.seen if s.path == LIST] == [["1"], ["2"]]
    assert front.paths().count(PAGE) == 1


async def test_shop_that_moved_is_read_at_its_new_address(front, tmp_path, chromium):
    """www.shop.example forwarding to shop.example, say: the list is asked of the
    address the page ended up at, or the browser would refuse the request."""
    port = front.url.rsplit(":", 1)[1]
    with Shopfront() as old:
        old.answers[PAGE] = (301, {"Location": f"http://localhost:{port}{PAGE}"}, "")
        async with browser.BrowserReader(tmp_path / "profile", chromium) as reader:
            items = await shops.fetch(reader, shop_at(old.url))
        assert old.paths() == [PAGE]
    assert len(items) == 3
    assert front.last(LIST).headers["host"] == f"localhost:{port}"


async def test_profile_left_locked_by_a_stopped_container_is_used_again(front, tmp_path, chromium):
    """Chromium marks a profile as in use with a link that names the machine. A container
    stopped mid-read leaves it behind, and the updated container has another name."""
    profile = tmp_path / "profile"
    await read(front, profile, chromium)
    os.symlink("0a1b2c3d4e5f-4242", profile / "SingletonLock")
    os.symlink("12345678901234567890", profile / "SingletonCookie")
    assert len(await read(front, profile, chromium)) == 3
    assert front.last(PAGE).headers["cookie"] == "visitor=1"  # the same profile, not a fresh one


# --- the tracker's side ---------------------------------------------------------

def browsing_tracker(tmp_path, monkeypatch, **settings) -> Tracker:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHOP_READER", "browser")
    s = replace(config.load(), ha_webhook_url=HA, news_enabled=False, **settings)
    return Tracker(s, DB(s.db_path))


class Stand:
    """Stands in for Chromium: each shop gives its list, or the listed answers in turn."""

    def __init__(self, monkeypatch):
        self.answers: dict[str, list] = {}
        self.started = 0
        self.asked: list[str] = []
        monkeypatch.setattr(browser, "BrowserReader", self.reader)

    def reader(self, profile, executable=""):
        stand = self

        class Reader:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                pass

            async def products(self, shop, page):
                stand.started += not stand.asked
                stand.asked.append(shop.key)
                steps = stand.answers.get(shop.key)
                answer = steps.pop(0) if steps else FEEDS[shop.key]
                if isinstance(answer, Exception):
                    raise answer
                return answer

        return Reader()


async def read_shops(tracker, due_only: bool = False) -> str:
    with respx.mock(assert_all_called=False) as router:
        router.post(HA).respond(200)
        return await tracker.run_shops(due_only=due_only)


def test_reader_setting(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("SHOP_READER", raising=False)
    assert config.load().shop_reader == "feed"  # the plain reader unless asked otherwise
    for value, reader in (("browser", "browser"), (" Browser ", "browser"), ("feed", "feed"), ("", "feed")):
        monkeypatch.setenv("SHOP_READER", value)
        assert config.load().shop_reader == reader
    monkeypatch.setenv("SHOP_READER", "chrome")
    assert config.load().shop_reader == "feed"
    assert "SHOP_READER=chrome is not one of feed, browser" in caplog.text

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHOP_READER", "feed")
    s = config.load()
    assert isinstance(Tracker(s, DB(s.db_path))._reader(), shops.FeedReader)
    monkeypatch.setenv("BROWSER_PATH", "/usr/bin/chromium")
    reader = browsing_tracker(tmp_path, monkeypatch)._reader()
    assert isinstance(reader, browser.BrowserReader)
    # The profile sits with the database, so cookies outlive an update of the container.
    assert reader.profile == tmp_path / "browser" and reader.executable == "/usr/bin/chromium"


async def test_browser_reads_the_shops_one_at_a_time_some_seconds_apart(clock, tmp_path, monkeypatch, waits):  # noqa: F811
    stand = Stand(monkeypatch)
    monkeypatch.setattr(browser, "SHOP_GAP", (15, 45))
    tracker = browsing_tracker(tmp_path, monkeypatch)
    assert await read_shops(tracker) == "Shops: 3 of 3 read, 0 alerts."
    assert stand.asked == ["gruv", "lionsgate", "mvd"]
    assert len(waits) == 2 and all(15 <= w <= 45 for w in waits) and waits[0] != waits[1]


def test_browser_reads_come_round_about_once_an_hour_and_not_on_the_dot(tmp_path, monkeypatch):
    tracker = browsing_tracker(tmp_path, monkeypatch)
    minutes = sorted(tracker._shop_interval().total_seconds() / 60 for _ in range(200))
    assert 55 <= minutes[0] < 57 and 63 < minutes[-1] <= 65


def test_browser_reads_the_shops_hourly_at_most(tmp_path, monkeypatch):
    """SHOP_POLL_MINUTES=10 is the plain reader's floor. A visitor doesn't reload a shop
    six times an hour all day, so the browser doesn't either."""
    monkeypatch.setenv("SHOP_POLL_MINUTES", "10")
    tracker = browsing_tracker(tmp_path, monkeypatch)
    assert tracker.shop_poll_minutes == 60
    assert min(tracker._shop_interval() for _ in range(200)) >= timedelta(minutes=55)
    monkeypatch.setenv("SHOP_POLL_MINUTES", "180")
    assert browsing_tracker(tmp_path, monkeypatch).shop_poll_minutes == 180  # less often is fine

    monkeypatch.setenv("SHOP_READER", "feed")
    monkeypatch.setenv("SHOP_POLL_MINUTES", "10")
    s = config.load()
    plain = Tracker(s, DB(s.db_path))
    assert plain.shop_poll_minutes == 10
    assert max(plain._shop_interval() for _ in range(200)) <= timedelta(minutes=11)


async def test_a_check_for_people_quiets_every_shop(clock, tmp_path, monkeypatch):  # noqa: F811
    stand = Stand(monkeypatch)
    tracker = browsing_tracker(tmp_path, monkeypatch)
    stand.answers["gruv"] = [shops.Challenged()]
    assert await read_shops(tracker) == (
        "Shops: 0 of 1 read, 0 alerts. "
        "Gruv asked for a check that only a person can pass: no shop is asked again until 12:30 UTC.")
    assert stand.asked == ["gruv"]  # the others were not put in front of the same check
    assert fails(tracker) == [1, 1, 1]
    assert last_error(tracker, "Gruv").endswith(
        " Throttled: Gruv asked for a check that only a person can pass, and the tracker does not try "
        "to get past that; no shop is asked again until 12:30 UTC")
    assert tracker.shops_quiet() == (NOON + timedelta(minutes=30), "Gruv",
                                     "asked for a check that only a person can pass")

    clock.at(10)
    assert await read_shops(tracker) == (
        "Shops: none asked this time, 0 alerts. Not asked: every shop is left alone until 12:30 UTC "
        "(Gruv asked for a check that only a person can pass).")
    assert stand.asked == ["gruv"]


async def test_no_browser_is_started_while_the_shops_are_left_alone(clock, tmp_path, monkeypatch):  # noqa: F811
    stand = Stand(monkeypatch)
    tracker = browsing_tracker(tmp_path, monkeypatch)
    stand.answers["gruv"] = [shops.Throttled(60)]
    await read_shops(tracker)
    clock.at(5)
    await read_shops(tracker)
    assert stand.started == 1


async def test_browser_that_keeps_being_refused_backs_off_up_to_a_day(clock, tmp_path, monkeypatch):  # noqa: F811
    """Further than the plain reader's four hours: what is being refused now is the
    owner's own address, in the kind of browser he uses himself."""
    stand = Stand(monkeypatch)
    tracker = browsing_tracker(tmp_path, monkeypatch)
    stand.answers = {key: [shops.Challenged("would not serve the browser") for _ in range(8)] for key in SHOP}
    minutes = 0
    for wait in (30, 60, 120, 240, 480, 960, 1440, 1440):
        clock.at(minutes)
        await read_shops(tracker)
        assert tracker.shops_quiet()[0] == clock.now + timedelta(minutes=wait)
        minutes += wait + 1
    assert len(stand.asked) == 8  # one page in each wait, and no more
    assert "would not serve the browser, and the tracker does not try to get past that (8 reads in a row now)" \
        in last_error(tracker, tracker.shops_quiet()[1])

    stand.answers = {}
    clock.at(minutes)
    assert await read_shops(tracker) == "Shops: 3 of 3 read, 0 alerts."
    stand.answers["mvd"] = [shops.Throttled(0)]
    clock.at(minutes + 61)
    await read_shops(tracker)
    assert tracker.shops_quiet()[0] == clock.now + timedelta(minutes=30)  # served in between: from the start


async def test_changing_the_reader_drops_the_wait_the_other_reader_earned(clock, tmp_path, monkeypatch):  # noqa: F811
    """Shopify refused the plain reader for hours while a browser on the same connection
    was served. Someone who switches to the browser shouldn't wait out the other's hours."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SHOP_READER", raising=False)
    s = replace(config.load(), ha_webhook_url=HA, news_enabled=False)

    def plain() -> Tracker:
        return Tracker(s, DB(s.db_path))

    def browsing() -> Tracker:
        return browsing_tracker(tmp_path, monkeypatch)

    # A database as two earlier versions leave it: one kept a wait per shop, the next one wait for all.
    db = DB(s.db_path)
    db.set_meta("shop_refused_until:mvd", (NOON + timedelta(minutes=25)).isoformat())
    assert plain().shops_quiet()[0] == NOON + timedelta(minutes=25)
    assert db.get_meta("shops_refusals") == "1"

    stand = Stand(monkeypatch)
    tracker = browsing()
    assert tracker.shops_quiet() is None
    assert browsing().shops_quiet() is None  # nor does the old wait come back at the next start
    stand.answers["gruv"] = [shops.Challenged()]
    await read_shops(tracker)
    assert tracker.shops_quiet()[0] == NOON + timedelta(minutes=30)  # a first refusal, not a second

    # The browser's own wait holds across a restart, as the plain reader's does...
    assert browsing().shops_quiet() == (NOON + timedelta(minutes=30), "Gruv",
                                        "asked for a check that only a person can pass")
    # ...and is dropped in its turn by going back.
    back = plain()
    assert back.shops_quiet() is None
    back._refused(SHOP["mvd"], NOON, shops.Throttled(0))
    assert plain().shops_quiet() == (NOON + timedelta(minutes=30), "MVD Shop", 'said "too many requests"')
    assert browsing().shops_quiet() is None


async def test_tracker_reads_three_shops_with_the_real_browser(tmp_path, monkeypatch, chromium):
    """The whole path: the timer's read, Chromium, three shops' pages, the alerts."""
    lists = {shop.feed: FEEDS[key]["products"] for key, shop in SHOP.items()}
    with Shopfront(lists) as front:
        monkeypatch.setattr(shops, "SHOPS", tuple(replace(shop, site=front.url) for shop in shops.SHOPS))
        tracker = browsing_tracker(tmp_path, monkeypatch, browser_path=chromium)
        assert await read_shops(tracker) == "Shops: 3 of 3 read, 0 alerts."  # a first read is quiet
        pages = [shop.page for shop in shops.SHOPS]
        assert pages == ["/collections/4k-steelbooks", "/collections/all", "/collections/steelbooks"]
        assert [p for p in front.paths() if p in pages] == pages  # each shop's page, one after the other
        assert [p for p in front.paths() if p in lists] == [shop.feed for shop in shops.SHOPS]
        assert fails(tracker) == [0, 0, 0]
        with tracker.db.conn() as c:
            assert c.execute("SELECT COUNT(*) FROM shop_items").fetchone()[0] == 3 + 4 + 2

        front.lists[SHOP["gruv"].feed] = plus(FEEDS["gruv"], CLAYFACE)["products"]
        with respx.mock(assert_all_called=False) as router:
            ha = router.post(HA).respond(200)
            assert await tracker.run_shops() == "Shops: 3 of 3 read, 1 alert."
            sent = [json.loads(call.request.content) for call in ha.calls]
        assert len(sent) == 1 and "Clayface" in sent[0]["message"] and sent[0]["store"] == "Gruv"
        assert front.visitors == 1  # the second read came back as the same visitor
    assert (tmp_path / "browser").is_dir()
