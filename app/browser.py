"""The shops read with a real browser: SHOP_READER=browser.

The plain reader (shops.FeedReader) asks a shop for its product list directly, and the
shops' platform turns that kind of reader away for long stretches (see app/shops.py).
This one does what the tracker's owner does when he looks himself. Chromium opens the
shop's page, with everything on it, in a profile that keeps its cookies from one visit
to the next. The page then asks its own shop for the product list, the way a
storefront's scripts ask for things, and the tracker reads the answer.

That is a visitor's load on a shop: one page and one list, one shop at a time, about
once an hour and never more often (MIN_POLL_MINUTES). It is still a program, and it
does not pretend otherwise. What it must stay clear of, whatever a shop answers:

- No disguise. The browser runs as Playwright starts it: its user agent says
  "HeadlessChrome" and ``navigator.webdriver`` is true, so a shop that wants to know
  can tell. No stealth plug-ins, no edited fingerprint, no borrowed user agent.
- No answering of checks meant for people (a captcha, a "verify you are human" page),
  by any means.
- No other addresses: no proxies, no rotation.
- A refusal is taken as one. "Too many requests", a check for people or a "forbidden"
  ends the read and quiets every shop, for longer each time, up to MAX_COOL_OFF
  (Tracker._refused). Nothing here asks a second time.

tests/test_browser.py holds these down with a real Chromium and a stand-in shop.
"""

import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

from . import shops

log = logging.getLogger(__name__)

PROFILE = "browser"  # the browser's profile (cookies, cache), a folder under DATA_DIR
SHOP_GAP = (15, 45)  # seconds between two shops, picked anew each time
JITTER = 5 * 60  # seconds either side of SHOP_POLL_MINUTES
MIN_POLL_MINUTES = 60  # SHOP_POLL_MINUTES may ask for less; the browser doesn't go below this
# A browser that is being refused is the owner's own address being refused, so it backs
# off further than the plain reader: half an hour doubles up to a day.
MAX_COOL_OFF = 24 * 60 * 60
PAGE_TIMEOUT = 60  # seconds for a shop's page to arrive
SETTLE_TIMEOUT = 20  # more seconds for its images and scripts; a slow one is not waited for
LIST_TIMEOUT = 45  # seconds for the product list
CACHE_BYTES = 100 * 1024 * 1024  # of the profile's disk cache
# Chromium marks a profile as in use with these. They name the machine, and a container
# gets a new name with every update, so one left by a container that was stopped
# mid-read would lock the profile for good. Only one read runs at a time
# (Tracker.shop_lock), so a lock found before starting is always a stale one.
PROFILE_LOCKS = ("SingletonLock", "SingletonCookie", "SingletonSocket")

# Run in the shop's page: ask this shop for one page of its product list.
READ_LIST = """async ([path, seconds]) => {
  const r = await fetch(new URL(path, location.origin), {signal: AbortSignal.timeout(seconds * 1000)});
  return {status: r.status, url: r.url, retryAfter: r.headers.get("retry-after"),
          mitigated: r.headers.get("cf-mitigated"), text: await r.text()};
}"""


def turned_away(status: int, retry_after: str | None, mitigated: str | None, url: str) -> None:
    """Raise when an answer is the shop's platform saying no. See the top of this file."""
    if status == 429:
        raise shops.Throttled(shops.retry_wait(retry_after))
    # Cloudflare marks its checks with a "cf-mitigated: challenge" header; Shopify sends
    # the visitor to /challenge for its own.
    if (mitigated or "").strip().lower() == "challenge" or urlsplit(url).path.rstrip("/") == "/challenge":
        raise shops.Challenged()
    if status in (401, 403):
        raise shops.Challenged("would not serve the browser")


def _plain(error: Exception) -> str:
    """Playwright's errors carry a log of calls after the first line; the first line says it."""
    lines = str(error).strip().splitlines()
    return lines[0].strip() if lines else type(error).__name__


class BrowserReader:
    """One read of the shops with Chromium. Use it as ``async with``: the browser is
    started when the first shop is actually asked, and closed at the end."""

    def __init__(self, profile: Path, executable: str = ""):
        self.profile = Path(profile)
        self.executable = executable  # another Chromium than the one Playwright installs
        self._playwright = None
        self._context = None
        self._page = None
        self._at: str | None = None  # the shop whose page is open
        self._error: type[Exception] = Exception
        self._timeout: type[Exception] = TimeoutError

    async def __aenter__(self) -> "BrowserReader":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def products(self, shop: shops.Shop, page: int):
        """One page of the shop's list, as the shop sent it. The shop's own page is opened first."""
        try:
            if self._at != shop.key:
                await self._visit(shop)
            return await self._read_list(shop, page)
        except shops.Throttled:
            raise
        except Exception:
            # Whatever went wrong, the next shop gets a browser that is known to be running.
            await self.close()
            raise

    async def close(self) -> None:
        context, playwright = self._context, self._playwright
        self._context = self._page = self._playwright = self._at = None
        try:
            if context:
                await asyncio.wait_for(context.close(), 30)  # writes the cookies to the profile
        except Exception as e:
            log.warning("the browser did not close cleanly: %s", _plain(e))
        finally:
            if playwright:
                try:
                    await asyncio.wait_for(playwright.stop(), 30)
                except Exception as e:
                    log.warning("the browser's driver did not stop cleanly: %s", _plain(e))

    async def _open(self) -> None:
        if self._context:
            return
        try:
            from playwright.async_api import Error, TimeoutError as Timeout, async_playwright
        except ImportError:
            raise RuntimeError("SHOP_READER=browser needs the playwright package and its Chromium "
                               "(both are in the Docker image)") from None
        self._error, self._timeout = Error, Timeout
        self.profile.mkdir(parents=True, exist_ok=True)
        for name in PROFILE_LOCKS:
            (self.profile / name).unlink(missing_ok=True)
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile),
                # The whole browser, not Playwright's cut-down "headless shell".
                channel="chromium",
                headless=True,
                executable_path=self.executable or None,
                # Neither changes what a shop sees: the first is for Docker's small
                # /dev/shm, the second keeps the profile from growing without end.
                args=["--disable-dev-shm-usage", f"--disk-cache-size={CACHE_BYTES}"],
            )
        except Error as e:
            await self.close()
            raise RuntimeError(f"the browser could not be started: {_plain(e)}") from None
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

    async def _visit(self, shop: shops.Shop) -> None:
        """Open the page a person would browse. Raises unless the shop served it."""
        await self._open()
        self._at = None
        try:
            response = await self._page.goto(shop.page_url, wait_until="domcontentloaded",
                                             timeout=PAGE_TIMEOUT * 1000)
            try:
                await self._page.wait_for_load_state("load", timeout=SETTLE_TIMEOUT * 1000)
            except self._timeout:
                pass  # the page is there; something on it is slow
        except self._error as e:
            raise RuntimeError(f"the browser could not open the shop's page: {_plain(e)}") from None
        status = response.status if response else 200
        headers = response.headers if response else {}
        log.info("%s: the browser opened %s and was answered %s", shop.name, self._page.url, status)
        turned_away(status, headers.get("retry-after"), headers.get("cf-mitigated"), self._page.url)
        if status >= 400:
            raise RuntimeError(f"the shop's page answered with an error ({status})")
        self._at = shop.key

    async def _read_list(self, shop: shops.Shop, page: int):
        path = f"{shop.feed}?limit={shops.PAGE_SIZE}&page={page}"
        try:
            answer = await asyncio.wait_for(self._page.evaluate(READ_LIST, [path, LIST_TIMEOUT]),
                                            LIST_TIMEOUT + 15)
        except asyncio.TimeoutError:
            raise RuntimeError("the shop's product list did not arrive in time") from None
        except self._error as e:
            raise RuntimeError(f"the shop's page could not read its product list: {_plain(e)}") from None
        log.info("%s: page %s of the product list was answered %s", shop.name, page, answer["status"])
        turned_away(answer["status"], answer["retryAfter"], answer["mitigated"], answer["url"])
        if answer["status"] >= 400:
            raise RuntimeError(f"the shop's product list answered with an error ({answer['status']})")
        try:
            return json.loads(answer["text"])
        except ValueError:
            raise RuntimeError("the shop's product list is not in the expected format") from None
