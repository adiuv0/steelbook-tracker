"""The shops' own product lists: parsing, and the alerts a change in them produces.

The fixtures follow the lists Gruv, Lionsgate Limited and MVD Shop served on
2026-11-20, cut down to a few products each.
"""

import copy
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from app import config, shops
from app.db import DB
from app.jobs import Tracker

FIX = Path(__file__).parent / "fixtures"
HA = "http://ha.local:8123/api/webhook/steelbooks"
SHOP = {shop.key: shop for shop in shops.SHOPS}
URLS = {key: shop.feed_url for key, shop in SHOP.items()}
FEEDS = {key: json.loads((FIX / f"shop_{key}.json").read_text(encoding="utf-8")) for key in SHOP}
DRAGON = "House of the Dragon"  # sold out at Gruv in the fixture


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = replace(config.load(), ha_webhook_url=HA, news_enabled=False)
    return Tracker(s, DB(s.db_path))


def product(pid: int, title: str, *, vendor: str = "DC Studios", available: bool = True,
            price: str = "37.95") -> dict:
    handle = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return {"id": pid, "title": title, "handle": handle, "vendor": vendor, "tags": ["UHD"],
            "published_at": "2026-11-20T09:00:00-08:00",
            "variants": [{"id": pid + 1, "available": available, "price": price}],
            "images": [{"src": f"https://cdn.shopify.com/s/files/{handle}.jpg"}]}


CLAYFACE = product(777, "Clayface (Limited Edition 4K Ultra HD Steelbook + Blu-ray + Digital) [UHD]")


def plus(feed: dict, *products: dict) -> dict:
    """The feed with more products at the top, the way a shop lists new arrivals."""
    return {"products": [*copy.deepcopy(products), *copy.deepcopy(feed["products"])]}


def stock(feed: dict, title_part: str, available: bool) -> dict:
    """The feed with one title's stock changed."""
    feed = copy.deepcopy(feed)
    hits = [p for p in feed["products"] if title_part in p["title"]]
    assert hits, title_part
    for p in hits:
        for v in p["variants"]:
            v["available"] = available
    return feed


async def check(tracker, ha_status: int = 200, **feeds) -> tuple[str, list[dict]]:
    """One shop check. Each shop serves its fixture unless ``feeds`` gives it something
    else: other JSON, or a whole httpx.Response. Returns the result line and what
    was sent to Home Assistant."""
    with respx.mock(assert_all_called=False) as router:
        for key, url in URLS.items():
            answer = feeds.get(key, FEEDS[key])
            if isinstance(answer, httpx.Response):
                router.get(url).mock(return_value=answer)
            else:
                router.get(url).respond(200, json=answer)
        ha = router.post(HA).respond(ha_status)
        result = await tracker.run_shops()
        return result, [json.loads(c.request.content) for c in ha.calls]


def item(tracker, shop: str, title_part: str):
    with tracker.db.conn() as c:
        return c.execute("SELECT * FROM shop_items WHERE shop=? AND title LIKE ?",
                         (shop, f"%{title_part}%")).fetchone()


def fail_count(tracker, source: str) -> int | None:
    return next((h["fail_count"] for h in tracker.db.health() if h["name"] == source), None)


# --- reading a list ---------------------------------------------------------

def test_gruv_products_are_read_with_studio_price_and_stock():
    items = shops.parse_products(SHOP["gruv"], FEEDS["gruv"]["products"])
    assert items[0] == {
        "id": 10487712022786,
        "title": "Prisoners Of The Ghostland (4K Ultra HD Steelbook)",  # Gruv's "[UHD]" code is dropped
        "url": "https://www.gruv.com/products/prisoners-of-the-ghostland-4k-ultra-hd-steelbook-uhd-_1000866575",
        "studio": "AMC",
        "price": "$27.57",
        "available": True,
        "preorder": False,
        "image": "https://cdn.shopify.com/s/files/1/0001/0002/0003/files/prisoners-of-the-ghostla.jpg?v=1763400000",
        "published": "2026-11-17T09:45:48+00:00",
    }
    assert [(i["title"].split(" (")[0], i["available"]) for i in items] == [
        ("Prisoners Of The Ghostland", True), (DRAGON, False), ("Wildwood", True)]


def test_lionsgate_list_is_cut_down_to_its_4k_steelbooks():
    """The whole shop is read: VHS tapes, collector's editions and a Blu-ray steelbook included."""
    items = {i["title"]: i for i in shops.parse_products(SHOP["lionsgate"], FEEDS["lionsgate"]["products"])}
    assert list(items) == [
        "Raw Deal 4K 40th Anniversary SteelBook®",
        "The Cabin in the Woods 4K SteelBook®",
        "Reservoir Dogs 4K SteelBook® Reprint - Mr. Pink Edition",
        "The Conversation 50th Anniversary Collectors Edition 4K SteelBook®",
    ]
    raw_deal = items["Raw Deal 4K 40th Anniversary SteelBook®"]
    assert raw_deal["available"] and raw_deal["preorder"] and raw_deal["price"] == "$34.99"
    assert raw_deal["studio"] is None  # the shop's "vendor" is its own name, not a studio
    assert raw_deal["url"] == "https://www.lionsgatelimited.com/products/raw-deal-4k-40th-anniversary-steelbook"
    # Sold out, though it still carries the plain "preorder" tag from when its pre-order was open.
    sold_out = items["Reservoir Dogs 4K SteelBook® Reprint - Mr. Pink Edition"]
    assert not sold_out["available"] and not sold_out["preorder"]


def test_mvd_marks_preorders_in_the_title_and_4k_in_a_tag():
    items = shops.parse_products(SHOP["mvd"], FEEDS["mvd"]["products"])
    assert [(i["title"], i["preorder"]) for i in items] == [
        ("Double Impact (Steelbook) (Limited Edition) (4K Ultra HD + Blu-ray)", True),
        ("The Big Combo (Standard Edition) (3-Disc Steelbook Variant A) (4K Ultra HD + Blu-ray) EXCLUSIVE", False),
    ]  # the Blu-ray + DVD steelbook is left out


def test_products_that_cannot_be_read_are_skipped_not_fatal():
    good = product(1, "Good One 4K Steelbook")
    no_variants = {**product(2, "No Variants 4K Steelbook"), "variants": []}
    odd_price = {**product(3, "Odd Price 4K Steelbook"), "variants": [{"available": True, "price": "call us"}]}
    string_tags = {**product(4, "String Tags Steelbook"), "tags": "Blu-ray, 4K UHD"}
    two_prices = {**product(5, "Two Prices 4K Steelbook"),
                  "variants": [{"available": False, "price": "19.99"}, {"available": True, "price": "24.99"}]}
    broken = [None, "text", {"title": "No Id 4K Steelbook", "handle": "x"}, {"id": 9, "title": "No Handle 4K Steelbook"},
              {"id": "abc", "title": "Bad Id 4K Steelbook", "handle": "y"}]
    items = {i["id"]: i for i in shops.parse_products(
        SHOP["gruv"], [good, no_variants, odd_price, string_tags, two_prices, *broken])}
    assert sorted(items) == [1, 2, 3, 4, 5]
    assert items[2]["available"] is False and items[2]["price"] is None
    assert items[3]["available"] is True and items[3]["price"] is None
    assert items[5]["price"] == "$24.99"  # the price of the one that can be ordered


def test_shops_setting(monkeypatch):
    monkeypatch.delenv("SHOPS", raising=False)
    assert config.load().shops == ("gruv", "lionsgate", "mvd")
    for raw, expected in (("gruv, MVD", ("gruv", "mvd")), ("", ()), ("none", ()), ("off", ())):
        monkeypatch.setenv("SHOPS", raw)
        assert config.load().shops == expected, raw
    assert [s.key for s in shops.enabled(("mvd", "walmart", "gruv"))] == ["gruv", "mvd"]  # unknown names are ignored
    monkeypatch.setenv("SHOP_POLL_MINUTES", "1")
    assert config.load().shop_poll_minutes == 10  # never more often than every ten minutes


# --- alerts -----------------------------------------------------------------

async def test_first_read_records_everything_without_an_alert(tracker):
    result, pushes = await check(tracker)

    assert result == "Shops: 3 of 3 read, 0 alerts."
    assert pushes == []
    assert tracker.db.shop_counts() == {"gruv": {"listed": 3, "available": 2},
                                        "lionsgate": {"listed": 4, "available": 2},
                                        "mvd": {"listed": 2, "available": 2}}
    assert {h["name"]: h["fail_count"] for h in tracker.db.health()} == {
        "Gruv": 0, "Lionsgate Limited": 0, "MVD Shop": 0}
    # What can be ordered, the shop's newest first. Sold-out titles are left out.
    in_stock = [r["title"] for r in tracker.db.shop_in_stock(SHOP)]
    assert in_stock[:2] == ["Prisoners Of The Ghostland (4K Ultra HD Steelbook)",
                            "Raw Deal 4K 40th Anniversary SteelBook®"]
    assert len(in_stock) == 6 and not any(DRAGON in t for t in in_stock)


async def test_new_listing_that_can_be_ordered_alerts_once(tracker):
    await check(tracker)

    result, pushes = await check(tracker, gruv=plus(FEEDS["gruv"], CLAYFACE))

    assert result == "Shops: 3 of 3 read, 1 alert."
    assert pushes == [{
        "type": "preorder",  # the type Home Assistant sends on the loud channel
        "title": "New 4K steelbook at Gruv",
        "message": "Clayface (Limited Edition 4K Ultra HD Steelbook + Blu-ray + Digital) · DC Studios · $37.95",
        "url": "https://www.gruv.com/products/clayface-limited-edition-4k-ultra-hd-steelbook-blu-ray-digital-uhd",
        "image": "https://cdn.shopify.com/s/files/clayface-limited-edition-4k-ultra-hd-steelbook-blu-ray-digital-uhd.jpg",
        "id": "gruv-777",
        "store": "Gruv",
    }]
    assert tracker.db.shop_in_stock(SHOP)[0]["title"].startswith("Clayface")  # newest on the page too

    result, pushes = await check(tracker, gruv=plus(FEEDS["gruv"], CLAYFACE))
    assert (result, pushes) == ("Shops: 3 of 3 read, 0 alerts.", [])


async def test_several_new_listings_at_one_shop_make_one_alert(tracker):
    """Gruv adds titles in batches (13 on 2026-11-17). That is one push, not thirteen."""
    await check(tracker)
    batch = [product(800 + n, f"Batch Film {n:02d} (4K Ultra HD Steelbook) [UHD]", vendor="AMC", price="19.99")
             for n in range(10)]
    preorder = product(900, "Late Show 4K SteelBook®", price="34.99")
    preorder["tags"] = ["status::pre-order"]

    result, pushes = await check(tracker, gruv=plus(FEEDS["gruv"], *batch),
                                 lionsgate=plus(FEEDS["lionsgate"], preorder))

    assert result == "Shops: 3 of 3 read, 2 alerts."
    gruv, lionsgate = pushes
    assert gruv["title"] == "10 new 4K steelbooks at Gruv"
    lines = gruv["message"].split("\n")
    assert lines[0] == "• Batch Film 00 (4K Ultra HD Steelbook) · AMC · $19.99"
    assert len(lines) == 9 and lines[-1] == "and 2 more"
    assert gruv["url"] == "https://www.gruv.com/collections/4k-steelbooks" and gruv["image"] is None
    assert lionsgate["title"] == "New 4K steelbook at Lionsgate Limited"
    assert lionsgate["message"] == "Late Show 4K SteelBook® · $34.99 · pre-order"


async def test_listing_that_cannot_be_ordered_yet_alerts_when_it_can(tracker):
    await check(tracker)
    coming = product(777, CLAYFACE["title"], available=False)

    result, pushes = await check(tracker, gruv=plus(FEEDS["gruv"], coming))
    assert (result, pushes) == ("Shops: 3 of 3 read, 0 alerts.", [])  # nothing to act on yet

    result, pushes = await check(tracker, gruv=plus(FEEDS["gruv"], CLAYFACE))
    assert [(p["type"], p["title"]) for p in pushes] == [("restock", "Available to order at Gruv")]
    assert pushes[0]["message"].startswith("Clayface") and pushes[0]["url"].endswith("-digital-uhd")


async def test_sold_out_title_coming_back_alerts_then_keeps_quiet_for_a_day(tracker):
    await check(tracker)
    back = stock(FEEDS["gruv"], DRAGON, True)

    _, pushes = await check(tracker, gruv=back)
    assert [(p["type"], p["title"], p["message"]) for p in pushes] == [
        ("restock", "Available to order at Gruv", "House of the Dragon (4K Ultra HD Steelbook) · HBO · $29.99")]

    # Stock that flickers out and back in within the day does not ping the phone again...
    assert (await check(tracker))[1] == []
    assert (await check(tracker, gruv=back))[1] == []
    assert item(tracker, "gruv", DRAGON)["available"] == 1

    # ...but a restock a day or more after the last alert does.
    await check(tracker)
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    with tracker.db.conn() as c:
        c.execute("UPDATE shop_items SET alerted_at=? WHERE title LIKE ?", (long_ago, f"%{DRAGON}%"))
    _, pushes = await check(tracker, gruv=back)
    assert [p["type"] for p in pushes] == ["restock"]


async def test_alert_is_kept_until_home_assistant_takes_it(tracker):
    await check(tracker)
    feed = plus(FEEDS["gruv"], CLAYFACE)

    result, pushes = await check(tracker, ha_status=500, gruv=feed)
    assert result == "Shops: 3 of 3 read, 0 alerts (not delivered: Home Assistant webhook failed, will retry)."
    assert item(tracker, "gruv", "Clayface")["alert"] == "new"

    result, pushes = await check(tracker, gruv=feed)
    assert result == "Shops: 3 of 3 read, 1 alert." and [p["title"] for p in pushes] == ["New 4K steelbook at Gruv"]
    assert (await check(tracker, gruv=feed))[1] == []


async def test_alert_still_waiting_is_dropped_when_the_title_sells_out(tracker):
    await check(tracker)
    await check(tracker, ha_status=500, gruv=plus(FEEDS["gruv"], CLAYFACE))

    gone = plus(FEEDS["gruv"], product(777, CLAYFACE["title"], available=False))
    assert (await check(tracker, gruv=gone))[1] == []
    assert item(tracker, "gruv", "Clayface")["alert"] is None


async def test_list_that_comes_back_cut_short_causes_no_alerts(tracker):
    """Products missing from a read are only marked as not listed. Treating them as
    sold out would turn one short answer into a burst of "available again" alerts."""
    await check(tracker)
    short = {"products": FEEDS["gruv"]["products"][:1]}

    assert (await check(tracker, gruv=short))[1] == []
    assert tracker.db.shop_counts()["gruv"] == {"listed": 1, "available": 1}
    assert item(tracker, "gruv", "Wildwood")["listed"] == 0

    assert (await check(tracker))[1] == []
    assert tracker.db.shop_counts()["gruv"] == {"listed": 3, "available": 2}


# --- when a shop can't be read ----------------------------------------------

async def test_one_shop_failing_is_reported_and_the_others_are_still_read(tracker):
    await check(tracker)
    down = httpx.Response(503)
    new_at_mvd = plus(FEEDS["mvd"], product(950, "Fresh One (Steelbook) (4K Ultra HD + Blu-ray)", price="38.97"))

    result, pushes = await check(tracker, gruv=down, mvd=new_at_mvd)
    assert result == "Shops: 2 of 3 read, 1 alert."
    assert [p["title"] for p in pushes] == ["New 4K steelbook at MVD Shop"]
    assert fail_count(tracker, "Gruv") == 1

    assert (await check(tracker, gruv=down, mvd=new_at_mvd))[1] == []
    _, pushes = await check(tracker, gruv=down, mvd=new_at_mvd)  # third failure in a row
    assert [(p["type"], p["title"]) for p in pushes] == [("health", "Steelbook tracker source failing")]
    assert pushes[0]["message"].startswith("Gruv has failed repeatedly: HTTPStatusError")
    assert (await check(tracker, gruv=down, mvd=new_at_mvd))[1] == []  # said once

    _, pushes = await check(tracker, mvd=new_at_mvd)
    assert [(p["type"], p["message"]) for p in pushes] == [("health", "Gruv is returning data again.")]
    assert fail_count(tracker, "Gruv") == 0


async def test_answer_that_is_not_a_product_list_counts_as_a_failure(tracker):
    """An empty or unreadable list must not pass for "the shop sells nothing"."""
    await check(tracker)
    for n, answer in enumerate(({"products": []}, {"errors": "Not Found"}, ["products"],
                                httpx.Response(200, text="<html>Just a moment...</html>")), start=1):
        result, pushes = await check(tracker, lionsgate=answer)
        assert result.startswith("Shops: 2 of 3 read"), answer
        assert fail_count(tracker, "Lionsgate Limited") == n, answer
    assert tracker.db.shop_counts()["lionsgate"] == {"listed": 4, "available": 2}  # untouched


async def test_long_list_is_read_page_by_page(tracker, no_waiting):
    full_page = {"products": [product(1000 + n, f"Film {n} (4K Ultra HD Steelbook) [UHD]") for n in range(250)]}
    with respx.mock(assert_all_called=False) as router:
        router.get(URLS["gruv"], params={"limit": "250", "page": "1"}).respond(200, json=full_page)
        second = router.get(URLS["gruv"], params={"limit": "250", "page": "2"}).respond(200, json=FEEDS["gruv"])
        lionsgate = router.get(URLS["lionsgate"]).respond(200, json=FEEDS["lionsgate"])
        router.get(URLS["mvd"]).respond(200, json=FEEDS["mvd"])
        router.post(HA).respond(200)
        await tracker.run_shops()
    assert second.call_count == 1
    assert tracker.db.shop_counts()["gruv"]["listed"] == 253
    assert lionsgate.call_count == 1  # a short first page is the whole list
    assert dict(lionsgate.calls[0].request.url.params) == {"limit": "250", "page": "1"}
    assert lionsgate.calls[0].request.headers["user-agent"] == "steelbook-tracker/1.0"


# --- switching shops off ----------------------------------------------------

async def test_shop_left_out_of_the_setting_is_not_read_and_its_failure_is_forgotten(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = replace(config.load(), ha_webhook_url=HA, news_enabled=False, shops=("mvd",))
    db = DB(s.db_path)
    for _ in range(3):
        db.record_failure("Gruv", "HTTPStatusError", 3)
    tracker = Tracker(s, db)
    assert db.health() == []

    with respx.mock(assert_all_called=False) as router:  # a request to any other shop raises
        router.get(URLS["mvd"]).respond(200, json=FEEDS["mvd"])
        result = await tracker.run_shops()
        asked = {call.request.url.host for call in router.calls}
    assert result == "Shops: 1 of 1 read, 0 alerts."
    assert asked == {"mvdshop.com"}


async def test_no_shops_means_no_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = replace(config.load(), ha_webhook_url=HA, news_enabled=False, shops=())
    tracker = Tracker(s, DB(s.db_path))
    with respx.mock:  # any request raises
        assert await tracker.run_shops() == "No shops are being watched."
