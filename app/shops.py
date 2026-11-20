"""Shops that publish their own catalogue: which 4K steelbooks they list and
whether each can be ordered right now.

Blu-ray.com only knows Amazon's stock, so this is the tracker's one view of what
is on sale anywhere else.

Gruv, Lionsgate Limited and MVD Shop run on Shopify, whose storefront serves the
products of any collection as JSON (``/collections/<handle>/products.json``, or
``/products.json`` for the whole shop), 250 to a page. Every product carries a
per-variant ``available`` flag: the shop's own answer to "can this be ordered".
None of the three shops' robots.txt files rules these addresses out, and one
request reads a whole shop, so it is asked for no more than a visitor's browser
would load.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

# These shops and feeds answer a plain client, so the tracker says what it is.
USER_AGENT = "steelbook-tracker/1.0"
PAGE_SIZE = 250
MAX_PAGES = 4  # 1,000 products; the largest list today (Gruv) has about 240


@dataclass(frozen=True)
class Shop:
    key: str   # short name used in the SHOPS setting and in the database
    name: str
    site: str  # product pages live under <site>/products/<handle>
    feed: str  # path of the product list
    page: str  # path of the page a person would browse
    vendor_is_studio: bool = False  # the "vendor" field names the film's studio

    @property
    def feed_url(self) -> str:
        return self.site + self.feed

    @property
    def page_url(self) -> str:
        return self.site + self.page


SHOPS = (
    # Universal, Warner Bros., Shout! Factory, AMC and others.
    Shop("gruv", "Gruv", "https://www.gruv.com", "/collections/4k-steelbooks/products.json",
         "/collections/4k-steelbooks", vendor_is_studio=True),
    # No steelbook collection, so the whole (small) shop is read and filtered by title.
    Shop("lionsgate", "Lionsgate Limited", "https://www.lionsgatelimited.com", "/products.json",
         "/collections/all"),
    Shop("mvd", "MVD Shop", "https://mvdshop.com", "/collections/steelbooks/products.json",
         "/collections/steelbooks"),
)
DEFAULT_KEYS = tuple(shop.key for shop in SHOPS)

STEELBOOK = re.compile(r"steel\s?book", re.IGNORECASE)
# "4K" in the title, or a format tag such as "UHD", "4K UHD" or "Format_4K Ultra HD".
FOUR_K = re.compile(r"(?<![a-z0-9])(4k|uhd|ultra\s?hd)(?![a-z0-9])", re.IGNORECASE)
# Lionsgate Limited's status tag. Its plain "preorder" tag stays on a title after it
# has sold out, so that one is not trusted.
PREORDER_TAGS = {"status::pre-order", "status::preorder"}
# Shop housekeeping at the end of a title: Gruv's format code, MVD's "PREORDER" marker.
TITLE_SUFFIX = re.compile(r"\s*(\[UHD\]|PREORDER)\s*$")


def enabled(keys) -> tuple[Shop, ...]:
    """The shops named in the SHOPS setting, in the order they are listed above."""
    wanted = {k.strip().lower() for k in keys if k.strip()}
    unknown = wanted - set(DEFAULT_KEYS)
    if unknown:
        log.warning("SHOPS names shops the tracker does not know: %s (known: %s)",
                    ", ".join(sorted(unknown)), ", ".join(DEFAULT_KEYS))
    return tuple(shop for shop in SHOPS if shop.key in wanted)


def _price(variants: list[dict]) -> str | None:
    """Lowest price that can actually be paid: an orderable variant's if there is one."""
    def lowest(rows):
        prices = []
        for v in rows:
            try:
                prices.append(float(v.get("price")))
            except (TypeError, ValueError):
                continue
        return min(prices) if prices else None

    best = lowest([v for v in variants if v.get("available")])
    if best is None:
        best = lowest(variants)
    return None if best is None else f"${best:.2f}"


def _when(stamp) -> str | None:
    """Shopify's local-time stamp ("2026-11-10T08:58:54-08:00") as UTC, like every other time stored."""
    try:
        return datetime.fromisoformat(str(stamp)).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def parse_products(shop: Shop, products: list[dict]) -> list[dict]:
    """The 4K steelbooks among a shop's products."""
    items = []
    for p in products:
        if not isinstance(p, dict):
            continue
        raw_title = " ".join(str(p.get("title") or "").split())
        tags = p.get("tags") or []
        if isinstance(tags, str):  # some shops send one comma-separated string
            tags = tags.split(",")
        tags = [str(t).strip() for t in tags]
        if not STEELBOOK.search(raw_title):
            continue
        if not (FOUR_K.search(raw_title) or any(FOUR_K.search(t) for t in tags)):
            continue  # a Blu-ray steelbook
        handle = str(p.get("handle") or "").strip()
        try:
            pid = int(p.get("id"))
        except (TypeError, ValueError):
            continue
        if not handle:
            continue
        variants = [v for v in (p.get("variants") or []) if isinstance(v, dict)]
        available = any(v.get("available") is True for v in variants)
        marked_preorder = raw_title.endswith("PREORDER") or any(t.lower() in PREORDER_TAGS for t in tags)
        title = raw_title
        while TITLE_SUFFIX.search(title):
            title = TITLE_SUFFIX.sub("", title)
        images = [i for i in (p.get("images") or []) if isinstance(i, dict)]
        image = str(images[0].get("src") or "") if images else ""
        vendor = str(p.get("vendor") or "").strip()
        items.append({
            "id": pid,
            "title": title or raw_title,
            "url": f"{shop.site}/products/{quote(handle)}",
            "studio": vendor if shop.vendor_is_studio and vendor else None,
            "price": _price(variants),
            "available": available,
            "preorder": marked_preorder,
            "image": image if image.startswith("https://") else None,
            "published": _when(p.get("published_at")),
        })
    return items


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                             timeout=30, follow_redirects=True)


async def fetch(http: httpx.AsyncClient, shop: Shop) -> list[dict]:
    """Every 4K steelbook the shop lists right now. Raises when the list can't be read."""
    products: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            await asyncio.sleep(1)
        r = await http.get(shop.feed_url, params={"limit": PAGE_SIZE, "page": page})
        r.raise_for_status()
        data = r.json()
        batch = data.get("products") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise RuntimeError("the shop's product list is not in the expected format")
        products.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    if not products:
        # An empty shop is far less likely than a moved or renamed collection.
        raise RuntimeError("the shop's product list came back empty")
    return parse_products(shop, products)
