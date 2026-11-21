"""Shops that publish their own catalogue: which 4K steelbooks they list and
whether each can be ordered right now.

Blu-ray.com only knows Amazon's stock, so this is the tracker's one view of what
is on sale anywhere else.

Gruv, Lionsgate Limited and MVD Shop run on Shopify, whose storefront serves the
products of any collection as JSON (``/collections/<handle>/products.json``, or
``/products.json`` for the whole shop), 250 to a page. Every product carries a
per-variant ``available`` flag: the shop's own answer to "can this be ordered".
None of the three shops' robots.txt files rules these addresses out.

Shopify does limit automated readers, and since May 2026 most strictly those that
don't sign their requests (shopify.dev changelog, "Bots and agents should identify
themselves via Web Bot Auth"). The tracker is one of those. A reader the platform has
had enough of is answered "429 Too Many Requests" with "Retry-After: 60". What was
seen on 2026-11-21 with the tracker's own image, from one home connection:

- The three shops asked inside one second: two of them refused, then (an hour later)
  all three. Asked five seconds apart, every five minutes: served 18 times out of 18.
- A reader that has been refused stays refused for much longer than the minute the
  answer names. Asked again three, four and six minutes later it was refused each
  time; left alone for twenty-three minutes after two refusals it was served.
- After a dozen refusals in a quarter of an hour, half an hour's quiet was not enough:
  asked again 26 minutes later, and then twice more at half-hour intervals, it was
  refused every time.
- Once a reader is being refused, the three shops refuse it together (21 answers out
  of 21 that afternoon). The refusal is about the reader, not about the shop.

So the rules here are: one page per shop per read, SHOP_GAP seconds between shops and
never the same shop twice within MIN_GAP. When a shop says "too many requests" the
read stops there, and no shop is asked for COOL_OFF (longer if the shop asks for
longer). Each refusal in a row doubles that, up to MAX_COOL_OFF, so that while it is
being refused the tracker makes one request per wait rather than three every half
hour. Tracker._check_shops applies the rules to every read, whatever started it: the
timer, a restart or the Scan now button.

That is all the tracker does about the limit. The limit is the platform's decision, so
the tracker is not to be dressed up as a browser, or made to connect differently, to
get past it. A shop that keeps refusing shows as failing.
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
SHOP_GAP = 5  # seconds between two shops; they share one platform and its limiter
PAGE_GAP = 5  # seconds between two pages of one list
MIN_GAP = 5 * 60  # seconds before the same shop may be asked again
COOL_OFF = 30 * 60  # seconds no shop is asked after one says "too many requests"...
MAX_COOL_OFF = 4 * 60 * 60  # ...doubled for each refusal in a row, up to this


class Throttled(Exception):
    """The shop's platform answered "too many requests".

    ``wait`` is how many seconds it asked the tracker to stay away (0 when it didn't say).
    """

    def __init__(self, wait: float, message: str = "the shop's platform said too many requests"):
        super().__init__(message)
        self.wait = wait


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


def _retry_wait(response: httpx.Response) -> float:
    """Seconds the shop asked us to stay away; 0 when it didn't say in seconds."""
    try:
        wait = float(response.headers.get("Retry-After", ""))
    except ValueError:  # missing, or a date instead of a number of seconds
        return 0
    return max(wait, 0)


async def _get_page(http: httpx.AsyncClient, shop: Shop, page: int) -> httpx.Response:
    """One page of the shop's list. "Too many requests" is not argued with: see COOL_OFF."""
    r = await http.get(shop.feed_url, params={"limit": PAGE_SIZE, "page": page})
    if r.status_code == 429:
        raise Throttled(_retry_wait(r))
    r.raise_for_status()
    return r


async def fetch(http: httpx.AsyncClient, shop: Shop) -> list[dict]:
    """Every 4K steelbook the shop lists right now. Raises when the list can't be read."""
    products: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            await asyncio.sleep(PAGE_GAP)
        r = await _get_page(http, shop, page)
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
