"""The scheduled work: scan sources, check product pages, send alerts and digests."""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from . import notify, reddit
from .bluray import BluRayClient, is_upcoming
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

SOURCE_SEARCH = "Blu-ray.com search"
SOURCE_PRODUCT = "Blu-ray.com product pages"
SOURCE_REDDIT = "r/steelbooks"


class Tracker:
    def __init__(self, settings: Settings, db: DB):
        self.s = settings
        self.db = db
        self.lock = asyncio.Lock()
        self.last_run: str | None = None
        self.last_result: str | None = None

    # --- public entry points ----------------------------------------------
    async def run_scan(self) -> str:
        if self.lock.locked():
            return "A scan is already running."
        async with self.lock:
            try:
                result = await self._scan()
            except Exception as e:  # never let one bad run kill the scheduler
                log.exception("scan failed")
                result = f"Scan failed: {e}"
            self.last_run = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.last_result = result
            log.info(result)
            return result

    async def send_digest(self, force: bool = False) -> str:
        releases = self.db.pending_digest()
        community = self.db.pending_community()
        if not releases and not community and not force:
            return "Nothing new for the digest."
        payload = notify.digest_payload(releases, community, self.s.app_url)
        if await notify.send(self.s.ha_webhook_url, payload) or not self.s.ha_webhook_url:
            self.db.mark_digest_sent([r["id"] for r in releases])
            self.db.mark_community_sent([c["id"] for c in community])
        return f"Digest: {len(releases)} releases, {len(community)} community posts."

    # --- internals --------------------------------------------------------
    async def _scan(self) -> str:
        seeding = self.db.get_meta("seeded") is None
        today = date.today()
        new_count = 0
        checked = 0

        async with BluRayClient(self.s.request_delay_min, self.s.request_delay_max) as br:
            # 1. Listings: newest additions (announcements) and newest preorders.
            try:
                new_count += await self._scan_listing(br, "recentlyadded", seeding, today)
                new_count += await self._scan_listing(br, "preordertimestamp", seeding, today, pages=1)
                await self._ok(SOURCE_SEARCH)
            except Exception as e:
                await self._fail(SOURCE_SEARCH, e)

            # 2. Product pages: confirm steelbook/4K and watch for preorders opening.
            recheck_before = (datetime.now(timezone.utc) - timedelta(hours=self.s.recheck_hours)).isoformat()
            limit = self.s.max_page_fetches * (5 if seeding else 1)
            rows = self.db.releases_needing_check(recheck_before, today.isoformat(), limit)
            failures = 0
            for row in rows:
                try:
                    product = await br.product(row["url"])
                    self.db.apply_product(row["id"], product)
                    checked += 1
                except Exception as e:
                    failures += 1
                    log.warning("product check failed for %s: %s", row["url"], e)
                    if failures >= 3:
                        await self._fail(SOURCE_PRODUCT, e)
                        break
            else:
                if rows:
                    await self._ok(SOURCE_PRODUCT)

        # 3. Community posts.
        if self.s.reddit_enabled:
            try:
                for post in await reddit.fetch():
                    self.db.insert_community(post, digest_sent=seeding)
                await self._ok(SOURCE_REDDIT)
            except Exception as e:
                await self._fail(SOURCE_REDDIT, e)

        # 4. Alerts.
        if seeding:
            alerts = self.db.pending_preorder_alerts()
            self.db.mark_preorder_notified([r["id"] for r in alerts])
            self.db.mark_digest_sent([r["id"] for r in self.db.pending_digest()])
            self.db.set_meta("seeded", datetime.now(timezone.utc).isoformat())
            counts = self.db.counts(today.isoformat())
            await notify.send(self.s.ha_webhook_url, {
                "type": "setup",
                "title": "Steelbook tracker is running",
                "message": f"Tracking {counts['upcoming']} upcoming 4K steelbooks, "
                           f"{counts['preorder']} already on preorder.",
                "url": self.s.app_url or "",
            })
            return f"Seeded: {new_count} listings, {checked} product pages checked."

        sent = 0
        for row in self.db.pending_preorder_alerts():
            if await notify.send(self.s.ha_webhook_url, notify.preorder_payload(row, self.s.app_url)) \
                    or not self.s.ha_webhook_url:
                self.db.mark_preorder_notified([row["id"]])
                self.db.mark_digest_sent([row["id"]])
                sent += 1
        return f"Scan done: {new_count} new listings, {checked} product pages checked, {sent} preorder alerts."

    async def _scan_listing(self, br: BluRayClient, sortby: str, seeding: bool, today: date,
                            pages: int | None = None) -> int:
        """Walk listing pages. While seeding, keep going until a page has nothing upcoming."""
        max_pages = pages or (self.s.seed_pages if seeding else self.s.scan_pages)
        new = 0
        for page in range(max_pages):
            items = await br.search(sortby, page)
            if page == 0 and not items:
                raise RuntimeError(f"search '{sortby}' returned no results; page layout may have changed")
            upcoming = 0
            for item in items:
                if " 4K" not in f" {item['title']}":
                    continue
                if not is_upcoming(item["release_date"], today):
                    continue
                upcoming += 1
                if self.db.get_release(item["id"]) is None:
                    self.db.insert_release(item, digest_sent=False)
                    new += 1
                else:
                    self.db.update_listing(item)
            if seeding and upcoming == 0:
                break
        return new

    async def _ok(self, source: str):
        if self.db.record_ok(source):
            await notify.send(self.s.ha_webhook_url, notify.health_payload(source, "", recovered=True))

    async def _fail(self, source: str, error: Exception):
        log.error("%s failed: %s", source, error)
        if self.db.record_failure(source, str(error), self.s.health_fail_threshold):
            await notify.send(self.s.ha_webhook_url, notify.health_payload(source, str(error)))
