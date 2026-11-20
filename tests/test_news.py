"""HD Report's steelbook headlines: reading the feed, and their place in the daily digest."""

import copy
from dataclasses import replace
from xml.etree import ElementTree as ET

import pytest
import respx

from app import config, news
from app.db import DB
from app.jobs import Tracker
from app.notify import digest_payload

from .test_tracker import HA, mock_sites, read, sent

FEED = read("news_hdreport.xml")
SOURCE = "HD Report headlines"


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = replace(config.load(), ha_webhook_url=HA, request_delay_min=0, request_delay_max=0,
                seed_pages=1, scan_pages=1, failure_backoff_seconds=0, shops=())
    return Tracker(s, DB(s.db_path))


def feed_with(*posts: tuple[int, str]) -> str:
    """The fixture feed with newer posts on top: (post number, headline) each."""
    root = ET.fromstring(FEED)
    channel = root.find("channel")
    first = list(channel).index(channel.find("item"))
    for offset, (number, title) in enumerate(posts):
        item = copy.deepcopy(channel.find("item"))
        item.find("title").text = title
        item.find("link").text = f"https://hd-report.com/2026/11/19/post-{number}/"
        item.find("guid").text = f"https://hd-report.com/?p={number}"
        item.find("pubDate").text = f"Thu, 19 Nov 2026 1{offset}:00:00 +0000"
        channel.insert(first + offset, item)
    return ET.tostring(root, encoding="unicode")


async def scan(tracker, feed) -> tuple[str, list[dict]]:
    """One scan with Blu-ray.com mocked as usual. ``feed`` is the feed's text, or a status code."""
    with respx.mock(assert_all_called=False) as router:
        ha = mock_sites(router, read("product_announced.html"))
        if isinstance(feed, int):
            router.get(news.FEED_URL).respond(feed)
        else:
            router.get(news.FEED_URL).respond(200, text=feed, headers={"Content-Type": "application/rss+xml"})
        result = await tracker.run_scan()
        return result, sent(ha)


async def digest(tracker, status: int = 200) -> tuple[str, list[dict]]:
    with respx.mock(assert_all_called=False) as router:
        ha = router.post(HA).respond(status)
        result = await tracker.send_digest()
        return result, sent(ha)


def test_feed_is_read_into_headline_link_and_time():
    posts = news.parse_feed(FEED)
    assert posts[0] == {
        "id": "https://hd-report.com/?p=102155",
        "title": "Wildwood 4k UHD, Blu-ray, DVD & SteelBook Release Dates & Artwork Revealed",
        "url": "https://hd-report.com/2026/11/16/wildwood-4k-uhd-blu-ray-dvd-amp-steelbook-artwork-revealed/",
        "published": "2026-11-17T03:18:41+00:00",
    }
    assert [p["title"] for p in posts[1:]] == [
        "The Odyssey 4k SteelBooks Are Sold Out. Here’s Where To Buy.",
        # This one's "&" arrives escaped twice.
        "Wildwood (2026) Is Up For Pre-order On 4k UHD, Blu-ray, & Digital Plus A Limited SteelBook",
    ]


def test_posts_that_cannot_be_linked_are_left_out():
    feed = """<rss><channel>
      <item><title>No link</title></item>
      <item><title>Not a web link</title><link>javascript:alert(1)</link></item>
      <item><title></title><link>https://example.com/untitled/</link></item>
      <item><title>No id or date</title><link>https://example.com/kept/</link><pubDate>someday</pubDate></item>
    </channel></rss>"""
    assert news.parse_feed(feed) == [{"id": "https://example.com/kept/", "title": "No id or date",
                                      "url": "https://example.com/kept/", "published": None}]


async def test_first_read_is_quiet_then_new_headlines_join_the_digest(tracker):
    result, _ = await scan(tracker, FEED)
    assert result.startswith("Seeded"), result
    assert [h["title"][:8] for h in tracker.db.recent_headlines()] == ["Wildwood", "The Odys", "Wildwood"]
    # A month of old headlines is on the page, not in the first digest.
    assert await digest(tracker) == ("Nothing new for the digest.", [])

    await scan(tracker, feed_with((102200, "Clayface 4k SteelBook Is Up For Pre-order At Walmart")))
    result, pushes = await digest(tracker)
    assert result == "Digest: 0 releases, 1 headline."
    assert pushes == [{"type": "digest", "title": "Steelbook news", "url": "", "count": 0,
                       "message": "• Clayface 4k SteelBook Is Up For Pre-order At Walmart"}]
    assert tracker.db.recent_headlines()[0]["title"].startswith("Clayface")

    assert await digest(tracker) == ("Nothing new for the digest.", [])  # sent once


async def test_headlines_wait_when_the_digest_is_not_delivered(tracker):
    await scan(tracker, FEED)
    await scan(tracker, feed_with((102200, "A Fresh Headline")))

    result, _ = await digest(tracker, status=500)
    assert "not delivered" in result
    result, pushes = await digest(tracker)
    assert result == "Digest: 0 releases, 1 headline." and pushes[0]["message"] == "• A Fresh Headline"


def test_headlines_follow_the_new_releases_in_the_digest():
    release = {"title": "Die Hard 4K", "edition": None, "release_date": "2026-12-01", "exclusive": None}
    headlines = [{"title": f"Headline {n}"} for n in range(1, 11)]

    payload = digest_payload([release], headlines[:2])
    assert payload["title"] == "1 new steelbook announced"
    assert payload["message"] == "• Die Hard 4K (2026-12-01)\n\nIn the news:\n• Headline 1\n• Headline 2"

    lines = digest_payload([], headlines)["message"].split("\n")
    assert lines[0] == "• Headline 1" and len(lines) == 9
    assert lines[-1] == "and 2 more headlines on the tracker page"


async def test_feed_failing_is_reported_and_the_scan_still_finishes(tracker):
    result, _ = await scan(tracker, 503)
    assert result.startswith("Seeded"), result
    await scan(tracker, 503)
    result, pushes = await scan(tracker, 503)

    assert result.startswith("Scan done"), result
    health = [p for p in pushes if p["type"] == "health"]
    assert len(health) == 1
    assert health[0]["message"].startswith(f"{SOURCE} has failed repeatedly: HTTPStatusError")

    _, pushes = await scan(tracker, FEED)
    assert [p["message"] for p in pushes if p["type"] == "health"] == [f"{SOURCE} is returning data again."]


async def test_feed_with_no_posts_counts_as_a_failure(tracker):
    """A moved feed can answer with an empty channel, as Reddit's did."""
    await scan(tracker, "<rss><channel><title>Not Found</title></channel></rss>")
    assert {h["name"]: h["fail_count"] for h in tracker.db.health()}[SOURCE] == 1
    await scan(tracker, "this is not xml")
    assert {h["name"]: h["fail_count"] for h in tracker.db.health()}[SOURCE] == 2


async def test_news_switched_off_is_not_read_or_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_ENABLED", "false")
    s = replace(config.load(), ha_webhook_url=HA, request_delay_min=0, request_delay_max=0,
                seed_pages=1, scan_pages=1, shops=())
    db = DB(s.db_path)
    db.record_failure(SOURCE, "HTTPStatusError", 3)
    db.insert_headline({"id": "1", "title": "Old", "url": "https://example.com/", "published": None},
                       digest_sent=False)
    tracker = Tracker(s, db)
    with respx.mock(assert_all_called=False) as router:  # a request to the feed would raise
        mock_sites(router, read("product_announced.html"))
        await tracker.run_scan()
    assert SOURCE not in {h["name"] for h in db.health()}
    assert await tracker.send_digest() == "Nothing new for the digest."
