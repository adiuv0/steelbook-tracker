"""The scheduled work: scan sources, check product pages, send alerts and digests."""

import asyncio
import logging
import random
from datetime import date, datetime, timedelta, timezone
from itertools import groupby

from . import news, notify, shops
from .bluray import BluRayClient, SessionError, is_upcoming
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

SOURCE_SEARCH = "Blu-ray.com search"
SOURCE_PRODUCT = "Blu-ray.com product pages"
SOURCE_HA = "Home Assistant webhook"
SOURCE_NEWS = f"{news.NAME} headlines"
# A shop's stock can flicker in and out; one "available again" alert a day per title is enough.
SHOP_REALERT_HOURS = 24


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clock(when: datetime) -> str:
    """A time of day for a message, in the container's timezone (TZ): "09:30 PST"."""
    return when.astimezone().strftime("%H:%M %Z")


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
        self.shops = shops.enabled(settings.shops)
        self.shop_lock = asyncio.Lock()
        self.last_shop_run: str | None = None
        self.last_shop_result: str | None = None
        # When the timer next reads each shop. Kept in memory: after a restart every shop
        # is due at once, and shop_wait() holds back the ones asked a moment ago.
        first = utcnow() if settings.run_on_start else utcnow() + self._shop_interval()
        self._shop_due = {shop.key: first for shop in self.shops}
        self._last_shop_request: datetime | None = None
        self._adopt_old_shop_waits()
        self.sources = (SOURCE_SEARCH, SOURCE_PRODUCT, SOURCE_HA,
                        *(shop.name for shop in self.shops),
                        *((SOURCE_NEWS,) if settings.news_enabled else ()))
        # A source that was dropped or switched off while it was failing (r/steelbooks,
        # when Reddit ended its RSS feeds) would otherwise stay listed as failing for good.
        self.db.forget_sources_except(self.sources)

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

    async def run_shops(self, due_only: bool = False) -> str:
        """Read the shops' own listings and send their alerts.

        One quick request per shop, so this runs on its own, shorter timer
        (SHOP_POLL_MINUTES) instead of waiting for the slow Blu-ray.com scan.
        The timer reads the shops that are due (``due_only``); Scan now asks for all
        of them. Either way a shop that was asked a moment ago is left alone, and so is
        every shop while one of them has said "too many requests": see shop_wait().
        """
        if not self.shops:
            return "No shops are being watched."
        if self.shop_lock.locked():
            return "A shop check is already running."
        now = utcnow()
        wanted = [shop for shop in self.shops if not due_only or self._shop_due[shop.key] <= now]
        if not wanted:
            return "No shop is due."
        async with self.shop_lock:
            try:
                result, asked = await self._check_shops(wanted)
            except Exception as e:  # never let one bad run kill the scheduler
                log.exception("shop check failed")
                result, asked = f"Shop check failed: {describe(e)}", True
            if asked:  # "last read" stays put when every shop was being left alone
                self.last_shop_run = utcnow().isoformat(timespec="seconds")
            self.last_shop_result = result
            log.info(result)
            return result

    def shops_quiet(self, now: datetime | None = None) -> tuple[datetime, str] | None:
        """Until when no shop is asked because one said "too many requests", and which one said it.

        None when that isn't so. The shops share one platform and it refuses them
        together, so one refusal quiets them all. Kept in the database, so a restart
        (an update of the container, say) doesn't ask again minutes after a refusal.
        """
        until = self._meta_time("shops_quiet_until")
        if until and until > (now or utcnow()):
            return until, self.db.get_meta("shops_quiet_by") or "One of them"
        return None

    def shop_wait(self, shop: shops.Shop, now: datetime | None = None) -> tuple[datetime, bool] | None:
        """Until when a shop is left alone, and whether that is because the tracker was refused.

        None when it may be asked now.
        """
        now = now or utcnow()
        quiet = self.shops_quiet(now)
        if quiet:
            return quiet[0], True
        again = self._may_ask_again(shop)
        if again and again > now:
            return again, False
        return None

    def _may_ask_again(self, shop: shops.Shop) -> datetime | None:
        """MIN_GAP after the shop was last asked; None if it never was."""
        asked = self._meta_time(f"shop_asked:{shop.key}")
        return asked + timedelta(seconds=shops.MIN_GAP) if asked else None

    def next_shop_delay(self) -> float:
        """Seconds until the timer should read the shops again."""
        now = utcnow()
        soonest = min(max(self._shop_due[shop.key], (self.shop_wait(shop, now) or (now, False))[0])
                      for shop in self.shops)
        # Never less than a few seconds, so a check that is already running isn't raced.
        return max(5.0, (soonest - now).total_seconds())

    def _shop_interval(self) -> timedelta:
        # Jitter so requests don't land on the same minute every time.
        return timedelta(seconds=self.s.shop_poll_minutes * 60 + random.uniform(-60, 60))

    def _meta_time(self, key: str) -> datetime | None:
        try:
            return datetime.fromisoformat(self.db.get_meta(key) or "")
        except ValueError:
            return None

    def _refused(self, shop: shops.Shop, now: datetime, wait: float) -> tuple[datetime, int]:
        """Note that a shop said "too many requests". Returns until when no shop is asked,
        and how many reads in a row have now ended that way.

        Each refusal in a row doubles the wait, up to MAX_COOL_OFF: a reader that goes
        on asking stays refused, so the tracker asks less and less until it is served.
        """
        try:
            count = int(self.db.get_meta("shops_refusals") or 0) + 1
        except ValueError:
            count = 1
        pause = min(shops.COOL_OFF * 2 ** (count - 1), shops.MAX_COOL_OFF)
        until = now + timedelta(seconds=max(pause, wait))
        self.db.set_meta("shops_refusals", str(count))
        self.db.set_meta("shops_quiet_until", until.isoformat(timespec="seconds"))
        self.db.set_meta("shops_quiet_by", shop.name)
        self.db.set_meta(f"shop_refused_at:{shop.key}", now.isoformat(timespec="seconds"))
        return until, count

    def _adopt_old_shop_waits(self) -> None:
        """An earlier version kept one wait per shop. Carry the longest over, so the
        update that brings this version doesn't ask a shop before its wait is up."""
        old = [(until, shop.name) for shop in shops.SHOPS
               if (until := self._meta_time(f"shop_refused_until:{shop.key}"))]
        if not old:
            return
        until, name = max(old)
        if until > utcnow():  # nothing newer can exist: no shop has been asked since
            self.db.set_meta("shops_quiet_until", until.isoformat(timespec="seconds"))
            self.db.set_meta("shops_quiet_by", name)
            self.db.set_meta("shops_refusals", self.db.get_meta("shops_refusals") or "1")

    async def send_digest(self, force: bool = False) -> str:
        releases = self.db.pending_digest()
        headlines = self.db.pending_headlines() if self.s.news_enabled else []
        if not releases and not headlines and not force:
            return "Nothing new for the digest."
        delivered = await self._notify(notify.digest_payload(releases, headlines, self.s.app_url))
        if delivered:
            self.db.mark_digest_sent([r["id"] for r in releases])
            self.db.mark_headlines_sent([h["id"] for h in headlines])
        news_text = f", {len(headlines)} headline{'s' if len(headlines) != 1 else ''}" if headlines else ""
        return f"Digest: {len(releases)} releases{news_text}{self._delivery_note(delivered)}."

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

                # 2. Product pages: confirm steelbook/4K, watch for preorders opening and
                #    for restocks, keep price, date and availability current until release.
                now = datetime.now(timezone.utc)

                def ago(hours: float) -> str:
                    return (now - timedelta(hours=hours)).isoformat(timespec="seconds")

                rows = self.db.releases_needing_check(
                    today=today.isoformat(),
                    recheck_before=ago(self.s.recheck_hours),
                    preorder_recheck_before=ago(self.s.preorder_recheck_hours),
                    just_opened=just_opened,
                    urgent_before=ago(self.s.poll_hours / 2),
                    restock_watch_from=(today - timedelta(days=self.s.restock_watch_days)).isoformat(),
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

        # 3. News headlines, for the daily digest.
        if self.s.news_enabled:
            await self._read_news()

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
        restocks = 0
        if delivered:
            for row in self.db.pending_restock_alerts():
                delivered = await self._notify(notify.restock_payload(row))
                if not delivered:
                    break
                self.db.mark_restock_notified([row["id"]])
                restocks += 1
        note = self._delivery_note(delivered) if sent or restocks or not delivered else ""
        restock_text = f", {restocks} restock alerts" if restocks else ""
        return (f"Scan done: {new_count} new listings, {checked} product pages checked, "
                f"{sent} preorder alerts{restock_text}{note}.")

    async def _read_news(self) -> None:
        try:
            posts = await news.fetch()
        except Exception as e:
            await self._fail(SOURCE_NEWS, e)
            return
        # The first read only records what is already there, so the next digest
        # doesn't open with a month of old headlines.
        first = self.db.get_meta("news_seeded") is None
        for post in posts:
            self.db.insert_headline(post, digest_sent=first)
        if first:
            self.db.set_meta("news_seeded", datetime.now(timezone.utc).isoformat())
        await self._ok(SOURCE_NEWS)

    async def _check_shops(self, wanted: list[shops.Shop]) -> tuple[str, int]:
        """Read the wanted shops that may be asked now. Returns what happened, and how many were asked."""
        read = 0
        asked = 0
        just_asked: list[str] = []
        quiet = None  # no shop was to be asked when this read began: (until when, who refused)
        stopped = None  # a shop refused during this read: (until when, the shop)
        realert_before = (datetime.now(timezone.utc)
                          - timedelta(hours=SHOP_REALERT_HOURS)).isoformat(timespec="seconds")
        next_read = utcnow() + self._shop_interval()
        # The shop that refused most recently goes last. If the refusal was about that one
        # shop, the others are read before it says no again; if it was about the tracker,
        # the next attempt goes to a different shop.
        order = sorted(wanted, key=lambda shop: self.db.get_meta(f"shop_refused_at:{shop.key}") or "")
        async with shops.client() as http:
            for shop in order:
                now = utcnow()
                if stopped:
                    # Not asked, so not read: a failed read like the one that refused. Unless
                    # it was read a moment ago by another read, and wouldn't have been asked.
                    if (self._may_ask_again(shop) or now) <= now:
                        await self._fail(shop.name, shops.Throttled(
                            0, f"not asked: {stopped[1].name} was just turned away, and the shops share "
                               f"one platform; no shop is asked again until {_clock(stopped[0])}"))
                    continue
                wait = self.shop_wait(shop, now)
                if wait:
                    if wait[1]:
                        quiet = self.shops_quiet(now)
                    else:
                        just_asked.append(f"{shop.name} until {_clock(wait[0])} (it was just asked)")
                    continue
                # A few seconds after the last request to any shop, whichever read made it.
                if self._last_shop_request:
                    since = max((now - self._last_shop_request).total_seconds(), 0)
                    if since < shops.SHOP_GAP:
                        await asyncio.sleep(shops.SHOP_GAP - since)
                        now = utcnow()
                asked += 1
                self.db.set_meta(f"shop_asked:{shop.key}", now.isoformat(timespec="seconds"))
                self._shop_due[shop.key] = next_read
                try:
                    items = await shops.fetch(http, shop)
                except shops.Throttled as e:
                    until, count = self._refused(shop, now, e.wait)
                    stopped = (until, shop)
                    await self._fail(shop.name, shops.Throttled(
                        e.wait, f"{shop.name}'s platform limits automated readers and turned the tracker away"
                                + (f" ({count} reads in a row now)" if count > 1 else "")
                                + f"; no shop is asked again until {_clock(until)}"))
                    continue
                except Exception as e:
                    await self._fail(shop.name, e)
                    continue
                finally:
                    self._last_shop_request = utcnow()
                # A shop's first read only records what it already sells.
                seeded_key = f"shop_seeded:{shop.key}"
                first = self.db.get_meta(seeded_key) is None
                self.db.apply_shop_items(shop.key, items, quiet=first, realert_before=realert_before)
                if first:
                    self.db.set_meta(seeded_key, datetime.now(timezone.utc).isoformat())
                if self.db.get_meta("shops_refusals"):
                    self.db.set_meta("shops_refusals", "")  # served: the next refusal starts from the short wait
                await self._ok(shop.name)
                read += 1

        by_key = {shop.key: shop for shop in self.shops}
        sent = 0
        delivered = True
        pending = self.db.pending_shop_alerts(by_key)
        for (key, kind), group in groupby(pending, key=lambda r: (r["shop"], r["alert"])):
            rows = list(group)
            delivered = await self._notify(notify.shop_payload(by_key[key], kind, rows))
            if not delivered:
                break  # Home Assistant isn't answering; these wait for the next check
            self.db.mark_shop_alerted(key, [r["id"] for r in rows])
            sent += 1
        note = self._delivery_note(delivered) if sent or not delivered else ""
        head = f"{read} of {asked} read" if asked else "none asked this time"
        result = f"Shops: {head}, {sent} alert{'s' if sent != 1 else ''}{note}."
        if stopped:
            result += f" {stopped[1].name} said too many requests: no shop is asked again until {_clock(stopped[0])}."
        elif quiet:
            result += f" Not asked: every shop is left alone until {_clock(quiet[0])} ({quiet[1]} said too many requests)."
        if just_asked:
            result += f" Not asked: {', '.join(just_asked)}."
        return result, asked

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
