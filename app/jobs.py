"""The scheduled work: scan sources, check product pages, send alerts and digests."""

import asyncio
import logging
import random
from datetime import date, datetime, timedelta, timezone

from . import notify, reddit
from .bluray import BluRayClient, SessionError, is_upcoming
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

SOURCE_SEARCH = "Blu-ray.com search"
SOURCE_PRODUCT = "Blu-ray.com product pages"
SOURCE_REDDIT = "r/steelbooks"
SOURCE_HA = "Home Assistant webhook"


def describe(error: Exception) -> str:
    """Error text for logs and alerts. Some errors (timeouts) carry no message."""
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__


class Tracker:
    def __init__(self, settings: Settings, db: DB):
        self.s = settings
        self.db = db
        self.lock = asyncio.Lock()
        self.last_run: str | None = None
        self.last_result: str | None = None
        self.stalled = 0  # scans in a row that left the backlog no smaller

    # --- public entry points ----------------------------------------------
    async def run_scan(self) -> str:
        if self.lock.locked():
            return "A scan is already running."
        async with self.lock:
            try:
                result = await self._scan()
            except Exception as e:  # never let one bad run kill the scheduler
                log.exception("scan failed")
                result = f"Scan failed: {describe(e)}"
                self.stalled += 1
            self.last_run = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.last_result = result
            log.info(result)
            return result

    async def send_digest(self, force: bool = False) -> str:
        releases = self.db.pending_digest()
        community = self.db.pending_community()
        if not releases and not community and not force:
            return "Nothing new for the digest."
        delivered = await self._notify(notify.digest_payload(releases, community, self.s.app_url))
        if delivered:
            self.db.mark_digest_sent([r["id"] for r in releases])
            self.db.mark_community_sent([c["id"] for c in community])
        return f"Digest: {len(releases)} releases, {len(community)} community posts{self._delivery_note(delivered)}."

    def has_backlog(self) -> bool:
        """True while the first scan is unfinished or listings still wait for their first check."""
        return self.db.get_meta("seeded") is None or self.db.unchecked_count() > 0

    def next_scan_delay(self) -> float:
        """Seconds to wait before the next scheduled scan."""
        regular = self.s.poll_hours * 3600
        if self.has_backlog():
            # Work is left over (e.g. a scan was cut short): come back soon instead of
            # waiting the full interval, but back off while that gets nowhere, so a
            # site that is refusing us isn't asked again every few minutes.
            return min(self.s.backlog_retry_minutes * 60 * 2 ** min(self.stalled, 10), regular)
        # Jitter so requests don't land on the same minute every time.
        return regular + random.uniform(-600, 600)

    # --- internals --------------------------------------------------------
    async def _scan(self) -> str:
        seeding = self.db.get_meta("seeded") is None
        today = date.today()
        new_count = 0
        checked = 0
        listed = False
        just_opened: set[int] = set()
        backlog_before = self.db.unchecked_count()

        try:
            async with BluRayClient(self.s.request_delay_min, self.s.request_delay_max) as br:
                # 1. Listings: newest additions (announcements) and newest preorders.
                try:
                    new_count += await self._scan_listing(br, "recentlyadded", seeding, today)
                    new_count += await self._scan_listing(br, "preordertimestamp", seeding, today,
                                                          pages=1, just_opened=just_opened)
                    await self._ok(SOURCE_SEARCH)
                    listed = True
                except Exception as e:
                    await self._fail(SOURCE_SEARCH, e)

                # 2. Product pages: confirm steelbook/4K, watch for preorders opening,
                #    keep price, date and availability current until release.
                now = datetime.now(timezone.utc)

                def ago(hours: float) -> str:
                    return (now - timedelta(hours=hours)).isoformat(timespec="seconds")

                rows = self.db.releases_needing_check(
                    today=today.isoformat(),
                    recheck_before=ago(self.s.recheck_hours),
                    preorder_recheck_before=ago(self.s.preorder_recheck_hours),
                    just_opened=just_opened,
                    just_opened_before=ago(self.s.poll_hours / 2),
                    limit=self.s.max_page_fetches * (5 if seeding else 1),
                )
                backlog_before = self.db.unchecked_count()
                checked = await self._check_products(br, rows)
        except SessionError as e:
            # Blu-ray.com can't be reached at all. Report it like any other source
            # failure and carry on with the rest of the run.
            await self._fail(SOURCE_SEARCH, e)

        backlog = self.db.unchecked_count()
        if (seeding and not listed) or (backlog and backlog >= backlog_before):
            self.stalled += 1
        else:
            self.stalled = 0

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
            if not listed:
                # Without the listings there is nothing to seed. Finishing here would
                # make the next scan announce every existing steelbook as new.
                return "First scan could not read Blu-ray.com's listings; it will be retried."
            alerts = self.db.pending_preorder_alerts()
            self.db.mark_preorder_notified([r["id"] for r in alerts])
            self.db.mark_digest_sent([r["id"] for r in self.db.pending_digest()])
            self.db.set_meta("seeded", datetime.now(timezone.utc).isoformat())
            counts = self.db.counts(today.isoformat())
            await self._notify({
                "type": "setup",
                "title": "Steelbook tracker is running",
                "message": f"Tracking {counts['upcoming']} upcoming 4K steelbooks, "
                           f"{counts['preorder']} already on preorder.",
                "url": self.s.app_url or "",
            })
            return f"Seeded: {new_count} listings, {checked} product pages checked."

        sent = 0
        delivered = True
        for row in self.db.pending_preorder_alerts():
            delivered = await self._notify(notify.preorder_payload(row))
            if not delivered:
                break  # Home Assistant isn't answering; the rest wait for the next scan too
            self.db.mark_preorder_notified([row["id"]])
            self.db.mark_digest_sent([row["id"]])
            sent += 1
        note = self._delivery_note(delivered) if sent or not delivered else ""
        return (f"Scan done: {new_count} new listings, {checked} product pages checked, "
                f"{sent} preorder alerts{note}.")

    async def _check_products(self, br: BluRayClient, rows) -> int:
        """Check product pages, riding out short outages instead of giving up.

        A failure pauses, opens a fresh session and carries on; only several
        failures in a row end the batch. Whatever is left is picked up by the
        follow-up scan (see has_backlog).
        """
        checked = 0
        consecutive = 0
        last_error: Exception | None = None
        for row in rows:
            try:
                product = await br.product(row["url"])
            except Exception as e:
                consecutive += 1
                last_error = e
                log.warning("product check failed for %s: %s", row["url"], describe(e))
                if consecutive >= 3:
                    break
                await asyncio.sleep(self.s.failure_backoff_seconds * consecutive)
                try:
                    await br.rewarm()
                except Exception as warm_error:
                    log.warning("could not reopen blu-ray.com session: %s", describe(warm_error))
                continue
            self.db.apply_product(row["id"], product)
            checked += 1
            consecutive = 0
        if last_error is not None and (consecutive >= 3 or checked == 0):
            # The batch was cut short, or not one page could be read.
            await self._fail(SOURCE_PRODUCT, last_error)
        elif checked:
            await self._ok(SOURCE_PRODUCT)
        return checked

    async def _scan_listing(self, br: BluRayClient, sortby: str, seeding: bool, today: date,
                            pages: int | None = None, just_opened: set[int] | None = None) -> int:
        """Walk listing pages. While seeding, keep going until a page has nothing upcoming.

        ``just_opened`` collects known listings that this page shows but that we don't
        have on preorder; pass it for the newest-preorders listing, where that means
        the preorder has only just opened and the product page is worth checking now.
        """
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
                known = self.db.get_release(item["id"])
                if known is None:
                    self.db.insert_release(item, seeding=seeding)
                    new += 1
                else:
                    self.db.update_listing(item)
                    if just_opened is not None and not known["preorder"]:
                        just_opened.add(item["id"])
            if seeding and upcoming == 0:
                break
        return new

    async def _notify(self, payload: dict) -> bool:
        """Send one notification. True when it went out, or when there is nowhere to
        send it (no webhook configured means log only, by design)."""
        if not self.s.ha_webhook_url:
            log.info("HA_WEBHOOK_URL not set; would have sent: %s", payload.get("title"))
            return True
        try:
            await notify.send(self.s.ha_webhook_url, payload)
        except Exception as e:  # a bad URL or Home Assistant being down must not stop a scan
            log.error("Home Assistant webhook failed: %s", describe(e))
            # Shown under Sources in the web UI. No alert: it would go to the same webhook.
            self.db.record_failure(SOURCE_HA, describe(e), self.s.health_fail_threshold)
            return False
        self.db.record_ok(SOURCE_HA)
        return True

    def _delivery_note(self, delivered: bool) -> str:
        if not delivered:
            return " (not delivered: Home Assistant webhook failed, will retry)"
        if not self.s.ha_webhook_url:
            return " (not sent: HA_WEBHOOK_URL is not set)"
        return ""

    async def _ok(self, source: str):
        if self.db.record_ok(source):
            await self._notify(notify.health_payload(source, "", recovered=True))

    async def _fail(self, source: str, error: Exception):
        log.error("%s failed: %s", source, describe(error))
        if self.db.record_failure(source, describe(error), self.s.health_fail_threshold):
            await self._notify(notify.health_payload(source, describe(error)))
