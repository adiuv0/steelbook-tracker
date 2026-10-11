"""Blu-ray.com client and parsers.

Blu-ray.com answers dynamic pages with the literal body ``error42`` unless the
request looks like a browser session: a cookie from the home page plus normal
Accept/Accept-Language/Referer headers. The client warms up a session first and
re-warms once if it sees that response.
"""

import asyncio
import logging
import random
import re
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE = "https://www.blu-ray.com"
SEARCH_URL = BASE + "/movies/search.php"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}

PRODUCT_URL_RE = re.compile(r"^https://www\.blu-ray\.com/movies/[^/]+/(\d+)/$")


class BlockedError(RuntimeError):
    """Blu-ray.com refused the request even after a fresh session."""


def parse_date(text: str | None) -> str | None:
    if not text:
        return None
    text = text.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def split_title(raw: str) -> tuple[str, str | None]:
    """'The Burbs 4K (Collector's Edition)' -> ('The Burbs 4K', "Collector's Edition")."""
    raw = _clean(raw)
    m = re.match(r"^(.*?)\s*\((.*)\)$", raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return raw, None


def parse_search(html: str) -> list[dict]:
    """Parse a search results page rendered with the 'simple' list layout."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    for td in soup.find_all("td", attrs={"width": "52%"}):
        a = td.find("a", href=True)
        if not a:
            continue
        m = PRODUCT_URL_RE.match(a["href"])
        if not m:
            continue
        row = td.find_parent("tr")
        cells = row.find_all("td") if row else []
        title, edition = split_title(a.get_text())
        year_el = td.find("font")
        year = _clean(year_el.get_text()).strip("()") if year_el else None
        studio = _clean(cells[2].get_text()) if len(cells) > 2 else None
        release = parse_date(cells[3].get_text()) if len(cells) > 3 else None
        items.append(
            {
                "id": int(m.group(1)),
                "title": title,
                "edition": edition,
                "studio": studio or None,
                "year": year or None,
                "release_date": release,
                "url": a["href"],
            }
        )
    return items


def parse_product(html: str) -> dict:
    """Pull the bits we need from a product page."""
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text()) if soup.title else ""
    subtitle_el = soup.find("span", class_="subheadingtitle")
    subtitle = _clean(subtitle_el.get_text()) if subtitle_el else ""
    is_steelbook = "steelbook" in title.lower() or "steelbook" in subtitle.lower()
    is_4k = " 4K " in f" {title} " or "4K Ultra HD" in subtitle

    release = None
    for a in soup.find_all("a", href=True):
        if "releasedates.php" in a["href"]:
            release = parse_date(a.get_text())
            if release:
                break

    cover = None
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        cover = og["content"]

    text = soup.get_text(" ")
    preorder = "Available for pre-order" in text

    price = None
    buy_url = None
    price_label = soup.find("span", class_="subheading", string="Price")
    if price_label:
        block = price_label.parent
        b = block.find("b") if block else None
        if b:
            price = _clean(b.get_text())
    buy = soup.find("a", id="movie_buylink")
    if buy and buy.get("href"):
        buy_url = buy["href"]

    return {
        "is_steelbook": int(is_steelbook and is_4k),
        "preorder": int(preorder),
        "price": price,
        "buy_url": buy_url,
        "cover": cover,
        "release_date": release,
        "subtitle": subtitle,
    }


class BluRayClient:
    def __init__(self, delay_min: float = 4, delay_max: float = 9):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True)
        await self._warm()
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()

    async def _warm(self):
        self._client.cookies.clear()
        r = await self._client.get(BASE + "/")
        r.raise_for_status()
        # 'simple' list layout gives one table row per result with the release date.
        self._client.cookies.set("listlayout_7", "simple", domain="www.blu-ray.com")
        await self._pause()

    async def _pause(self):
        await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))

    async def get(self, url: str, params: dict | None = None) -> str:
        for attempt in (1, 2):
            r = await self._client.get(url, params=params)
            body = r.text
            blocked = r.status_code in (403, 429, 503) or body.strip() == "error42"
            if not blocked:
                r.raise_for_status()
                await self._pause()
                return body
            log.warning("blu-ray.com refused %s (status %s), attempt %d", url, r.status_code, attempt)
            if attempt == 1:
                await asyncio.sleep(30)
                await self._warm()
        raise BlockedError(f"blu-ray.com refused {url}")

    async def search(self, sortby: str, page: int, keyword: str = "4k steelbook") -> list[dict]:
        html = await self.get(
            SEARCH_URL,
            params={"action": "search", "keyword": keyword, "sortby": sortby, "page": page},
        )
        return parse_search(html)

    async def product(self, url: str) -> dict:
        return parse_product(await self.get(url))


def is_upcoming(release_date: str | None, today: date | None = None) -> bool:
    if release_date is None:
        return True
    return release_date >= (today or date.today()).isoformat()
