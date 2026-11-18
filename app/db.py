"""SQLite storage. One small file under DATA_DIR, safe to back up by copying."""

import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    id                INTEGER PRIMARY KEY,   -- Blu-ray.com product id
    title             TEXT NOT NULL,
    edition           TEXT,
    studio            TEXT,
    year              TEXT,
    release_date      TEXT,                  -- ISO date or NULL when unannounced
    url               TEXT NOT NULL,
    cover             TEXT,
    is_steelbook      INTEGER,               -- NULL until the product page is checked
    preorder          INTEGER NOT NULL DEFAULT 0,
    price             TEXT,
    availability      TEXT,                  -- e.g. 'Available for pre-order', 'Currently unavailable'
    unavailable       INTEGER NOT NULL DEFAULT 0, -- the store last said it cannot be ordered
    restock_pending   INTEGER NOT NULL DEFAULT 0, -- orderable again; alert not sent yet
    exclusive         TEXT,                  -- the one store that sells this edition, e.g. 'Amazon'
    buy_url           TEXT,
    first_seen        TEXT NOT NULL,
    last_checked      TEXT,
    preorder_seen_at  TEXT,
    purchased         INTEGER NOT NULL DEFAULT 0,
    hidden            INTEGER NOT NULL DEFAULT 0,
    digest_sent       INTEGER NOT NULL DEFAULT 0,
    preorder_notified INTEGER NOT NULL DEFAULT 0,
    seeded            INTEGER NOT NULL DEFAULT 0  -- found during the first (quiet) scan
);
CREATE INDEX IF NOT EXISTS idx_releases_date ON releases(release_date);

-- Databases created before November 2026 also hold a `community` table of
-- r/steelbooks posts. Nothing reads it any more.

CREATE TABLE IF NOT EXISTS source_health (
    name       TEXT PRIMARY KEY,
    last_ok    TEXT,
    last_error TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    alerted    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# Columns added after the first release; created on startup if missing.
MIGRATIONS = [
    ("availability", "TEXT"),
    ("seeded", "INTEGER NOT NULL DEFAULT 0"),
    ("unavailable", "INTEGER NOT NULL DEFAULT 0"),
    ("restock_pending", "INTEGER NOT NULL DEFAULT 0"),
    ("exclusive", "TEXT"),
]

# Rows not yet confirmed on their product page are shown too, flagged as unverified,
# so a slow or interrupted check never hides a listing.
LISTED = "(is_steelbook=1 OR is_steelbook IS NULL)"
# Not out yet. Takes a :today parameter.
UPCOMING = "(release_date IS NULL OR release_date >= :today)"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(releases)")}
            for name, ddl in MIGRATIONS:
                if name not in cols:
                    c.execute(f"ALTER TABLE releases ADD COLUMN {name} {ddl}")
            if "unavailable" not in cols:
                # Titles already known to be unavailable are watched for a restock from day one.
                c.execute("UPDATE releases SET unavailable=1 WHERE lower(availability) LIKE '%unavailable%'")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # --- meta -------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # --- releases ---------------------------------------------------------
    def get_release(self, rid: int) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM releases WHERE id=?", (rid,)).fetchone()

    def insert_release(self, item: dict, *, seeding: bool) -> None:
        """Rows found while seeding go straight to 'already announced' (no digest)."""
        with self.conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO releases
                   (id, title, edition, studio, year, release_date, url, first_seen, digest_sent, seeded)
                   VALUES (:id, :title, :edition, :studio, :year, :release_date, :url, :first_seen,
                           :seeded, :seeded)""",
                {**item, "first_seen": now_iso(), "seeded": int(seeding)},
            )

    def update_listing(self, item: dict) -> None:
        """Refresh fields the search listing knows about (release dates move)."""
        with self.conn() as c:
            c.execute(
                """UPDATE releases SET title=:title, edition=:edition, studio=:studio,
                   year=:year, url=:url, release_date=COALESCE(:release_date, release_date)
                   WHERE id=:id""",
                item,
            )

    def apply_product(self, rid: int, product: dict) -> None:
        with self.conn() as c:
            old = c.execute(
                """SELECT seeded, last_checked, preorder_notified, unavailable, restock_pending
                   FROM releases WHERE id=?""", (rid,)).fetchone()
            if old is None:
                return
            notified = old["preorder_notified"]
            if old["seeded"] and old["last_checked"] is None and product["preorder"]:
                # A seeded listing that is already on preorder the first time it is checked
                # was on preorder before the tracker existed: record it without an alert.
                notified = 1
            unavailable, restock = old["unavailable"], old["restock_pending"]
            if product["orderable"] is False:
                unavailable, restock = 1, 0  # (gone again before a pending alert went out)
            elif product["orderable"]:
                if unavailable:
                    # It can be ordered again. A preorder that has never been announced
                    # gets its normal "preorder live" alert instead of a second one.
                    restock = 0 if product["preorder"] and not notified else 1
                unavailable = 0
            # No store status on the page (None): nothing is known, so nothing changes.
            c.execute(
                """UPDATE releases SET is_steelbook=:is_steelbook, preorder=:preorder,
                   price=:price, availability=:availability, exclusive=:exclusive,
                   buy_url=:buy_url, cover=COALESCE(:cover, cover),
                   release_date=COALESCE(:release_date, release_date), last_checked=:now,
                   preorder_seen_at=CASE WHEN :preorder=1 AND preorder_seen_at IS NULL
                                         THEN :now ELSE preorder_seen_at END,
                   preorder_notified=:notified, unavailable=:unavailable, restock_pending=:restock
                   WHERE id=:id""",
                {**product, "id": rid, "now": now_iso(), "notified": notified,
                 "unavailable": unavailable, "restock": restock},
            )

    def releases_needing_check(self, *, today: str, recheck_before: str, preorder_recheck_before: str,
                               limit: int, just_opened: Iterable[int] = (), urgent_before: str = "",
                               restock_watch_from: str | None = None) -> list[sqlite3.Row]:
        """Rows due a product-page check, most urgent first.

        1. Rows where a change is expected and timing matters. Skipped if checked
           since ``urgent_before``, so follow-up scans don't refetch them.
           - Rows in ``just_opened``: Blu-ray.com lists them among its newest
             preorders while we still show them as not on preorder.
           - Rows that cannot be ordered right now, to catch a restock. These are
             watched past their release, back to ``restock_watch_from``.
        2. Rows never checked, newest listing first.
        3. Upcoming steelbooks last checked before their cutoff: ``recheck_before``
           while not on preorder, ``preorder_recheck_before`` once they are (price,
           date and availability still move, just less urgently).
        """
        ids = sorted(set(just_opened))
        listed = f"(id IN ({','.join(f':id{i}' for i in range(len(ids)))}) AND preorder=0)" if ids else "0"
        urgent = f"""(({listed} OR (unavailable=1 AND (release_date IS NULL OR release_date >= :watch_from)))
                      AND last_checked < :urgent_before)"""
        params = {
            **{f"id{i}": rid for i, rid in enumerate(ids)},
            "today": today, "watch_from": restock_watch_from or today, "urgent_before": urgent_before,
            "recheck_before": recheck_before, "preorder_recheck_before": preorder_recheck_before,
            "limit": limit,
        }
        with self.conn() as c:
            return c.execute(
                f"""SELECT * FROM releases
                    WHERE hidden=0 AND purchased=0 AND (
                        last_checked IS NULL OR (is_steelbook=1 AND (
                            {urgent}
                            OR ({UPCOMING} AND last_checked < CASE WHEN preorder=1
                                THEN :preorder_recheck_before ELSE :recheck_before END))))
                    ORDER BY COALESCE({urgent}, 0) DESC, last_checked IS NOT NULL, last_checked, id DESC
                    LIMIT :limit""",
                params,
            ).fetchall()

    def unchecked_count(self) -> int:
        with self.conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM releases WHERE last_checked IS NULL AND hidden=0 AND purchased=0"
            ).fetchone()[0]

    def pending_preorder_alerts(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                """SELECT * FROM releases WHERE is_steelbook=1 AND preorder=1
                   AND preorder_notified=0 AND hidden=0 AND purchased=0"""
            ).fetchall()

    def mark_preorder_notified(self, ids: list[int]) -> None:
        with self.conn() as c:
            c.executemany("UPDATE releases SET preorder_notified=1 WHERE id=?", [(i,) for i in ids])

    def pending_restock_alerts(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                """SELECT * FROM releases WHERE is_steelbook=1 AND restock_pending=1
                   AND hidden=0 AND purchased=0"""
            ).fetchall()

    def mark_restock_notified(self, ids: list[int]) -> None:
        with self.conn() as c:
            c.executemany("UPDATE releases SET restock_pending=0 WHERE id=?", [(i,) for i in ids])

    def pending_digest(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                """SELECT * FROM releases WHERE is_steelbook=1 AND digest_sent=0 AND hidden=0
                   ORDER BY release_date IS NULL, release_date"""
            ).fetchall()

    def mark_digest_sent(self, ids: list[int]) -> None:
        with self.conn() as c:
            c.executemany("UPDATE releases SET digest_sent=1 WHERE id=?", [(i,) for i in ids])

    def set_flag(self, rid: int, flag: str, value: bool) -> None:
        if flag not in {"purchased", "hidden"}:
            raise ValueError(flag)
        with self.conn() as c:
            c.execute(f"UPDATE releases SET {flag}=? WHERE id=?", (int(value), rid))

    def list_releases(self, view: str, today: str) -> list[sqlite3.Row]:
        where = {
            "upcoming": f"hidden=0 AND purchased=0 AND {UPCOMING}",
            # A preorder stops being one on release day; the flag itself is frozen
            # then, because released titles are no longer rechecked.
            "preorder": f"hidden=0 AND purchased=0 AND preorder=1 AND {UPCOMING}",
            "purchased": "purchased=1",
            "hidden": "hidden=1",
            "all": "1=1",
        }.get(view, "hidden=0 AND purchased=0")
        with self.conn() as c:
            return c.execute(
                f"""SELECT * FROM releases WHERE {LISTED} AND {where}
                    ORDER BY release_date IS NULL, release_date, title""",
                {"today": today},
            ).fetchall()

    def counts(self, today: str) -> dict:
        with self.conn() as c:
            row = c.execute(
                f"""SELECT
                      SUM(hidden=0 AND purchased=0 AND {UPCOMING}) AS upcoming,
                      SUM(hidden=0 AND purchased=0 AND preorder=1 AND {UPCOMING}) AS preorder,
                      SUM(purchased=1) AS purchased,
                      SUM(hidden=1) AS hidden,
                      SUM(is_steelbook IS NULL) AS unverified,
                      COUNT(*) AS total
                    FROM releases WHERE {LISTED}""",
                {"today": today},
            ).fetchone()
            return {k: (row[k] or 0) for k in row.keys()}

    # --- source health ----------------------------------------------------
    def record_ok(self, name: str) -> bool:
        """Returns True when the source had been flagged broken and just recovered."""
        with self.conn() as c:
            row = c.execute("SELECT alerted FROM source_health WHERE name=?", (name,)).fetchone()
            c.execute(
                """INSERT INTO source_health(name, last_ok, fail_count, alerted) VALUES(?, ?, 0, 0)
                   ON CONFLICT(name) DO UPDATE SET last_ok=excluded.last_ok, fail_count=0, alerted=0""",
                (name, now_iso()),
            )
            return bool(row and row["alerted"])

    def record_failure(self, name: str, error: str, threshold: int) -> bool:
        """Returns True exactly once, when the failure count first reaches the threshold."""
        with self.conn() as c:
            c.execute(
                """INSERT INTO source_health(name, last_error, fail_count) VALUES(?, ?, 1)
                   ON CONFLICT(name) DO UPDATE SET last_error=excluded.last_error,
                   fail_count=fail_count+1""",
                (name, f"{now_iso()} {error}"[:500]),
            )
            row = c.execute(
                "SELECT fail_count, alerted FROM source_health WHERE name=?", (name,)
            ).fetchone()
            if row["fail_count"] >= threshold and not row["alerted"]:
                c.execute("UPDATE source_health SET alerted=1 WHERE name=?", (name,))
                return True
            return False

    def health(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM source_health ORDER BY name").fetchall()

    def forget_sources_except(self, names: Iterable[str]) -> None:
        """Drop the health records of sources that are no longer read."""
        names = list(names)
        with self.conn() as c:
            c.execute(
                f"DELETE FROM source_health WHERE name NOT IN ({','.join('?' * len(names))})", names
            )
