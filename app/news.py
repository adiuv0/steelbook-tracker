"""HD Report's steelbook headlines, for the daily digest.

HD Report tags its posts about steelbooks (preorders opening, artwork reveals,
sell-outs, deals) whatever the store, Walmart included, and publishes the tag as
an ordinary RSS feed. Only the headline and the link to the article are kept.
"""

import html
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

from .shops import USER_AGENT

NAME = "HD Report"
FEED_URL = "https://hd-report.com/tag/steelbook/feed/"
SITE_URL = "https://hd-report.com/tag/steelbook/"
MAX_BYTES = 2_000_000  # the feed is about 30 KB; anything huge is not the feed


def _published(stamp: str | None) -> str | None:
    """RSS's "Tue, 17 Nov 2026 03:18:41 +0000" as an ISO time in UTC."""
    try:
        when = parsedate_to_datetime(stamp or "")
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    posts = []
    for item in root.iter("item"):
        # WordPress escapes "&" twice in some titles.
        title = " ".join(html.unescape(item.findtext("title") or "").split())
        url = (item.findtext("link") or "").strip()
        if not title or not url.startswith(("https://", "http://")):
            continue
        posts.append({
            "id": (item.findtext("guid") or "").strip() or url,
            "title": title,
            "url": url,
            "published": _published(item.findtext("pubDate")),
        })
    return posts


async def fetch() -> list[dict]:
    """The feed's current posts, newest first. Raises when it can't be read."""
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30,
                                 follow_redirects=True) as client:
        r = await client.get(FEED_URL)
        r.raise_for_status()
    if len(r.content) > MAX_BYTES:
        raise RuntimeError(f"the feed is {len(r.content)} bytes, far larger than expected")
    posts = parse_feed(r.text)
    if not posts:
        raise RuntimeError("the feed has no posts; its address or format may have changed")
    return posts
