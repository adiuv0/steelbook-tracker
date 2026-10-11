"""r/steelbooks RSS: community posts about drops, restocks and preorders.

The subreddit is mostly collection photos, so only titles that mention a store
or an availability keyword are kept.
"""

import re
import xml.etree.ElementTree as ET

import httpx

from .bluray import USER_AGENT

FEED_URL = "https://www.reddit.com/r/steelbooks/new/.rss"
ATOM = "{http://www.w3.org/2005/Atom}"

KEYWORDS = re.compile(
    r"pre-?order|in stock|restock|back in stock|live now|is live|available|announce|"
    r"walmart|target|amazon|zavvi|best ?buy|exclusive|drop",
    re.IGNORECASE,
)
# Hauls and shelf photos mention stores too; skip the obvious ones.
NOISE = re.compile(r"\b(haul|collection|my shelf|wall|pick ?up|picked up|delivery|scored|custom)\b", re.IGNORECASE)


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    posts = []
    for entry in root.findall(f"{ATOM}entry"):
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        link_el = entry.find(f"{ATOM}link")
        posts.append(
            {
                "id": (entry.findtext(f"{ATOM}id") or "").strip(),
                "title": title,
                "url": link_el.get("href") if link_el is not None else "",
                "published": (entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated") or "").strip(),
            }
        )
    return [p for p in posts if p["id"] and p["url"]]


def is_relevant(title: str) -> bool:
    return bool(KEYWORDS.search(title)) and not NOISE.search(title)


async def fetch() -> list[dict]:
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as client:
        r = await client.get(FEED_URL)
        r.raise_for_status()
    return [p for p in parse_feed(r.text) if is_relevant(p["title"])]
