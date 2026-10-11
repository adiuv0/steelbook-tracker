"""SQLite storage. One small file under DATA_DIR, safe to back up by copying."""

import sqlite3
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
    buy_url           TEXT,
    first_seen        TEXT NOT NULL,
    last_checked      TEXT,
    preorder_seen_at  TEXT,
    purchased         INTEGER NOT NULL DEFAULT 0,
    hidden            INTEGER NOT NULL DEFAULT 0,
    digest_sent       INTEGER NOT NULL DEFAULT 0,
    preorder_notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_releases_date ON releases(release_date);

CREATE TABLE IF NOT EXISTS community (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    published   TEXT,
    first_seen  TEXT NOT NULL,
    digest_sent INTEGER NOT NULL DEFAULT 0
);

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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(releases)")}
            if "availability" not in cols:
                c.execute("ALTER TABLE releases ADD COLUMN availability TEXT")

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

    def insert_release(self, item: dict, *, digest_sent: bool) -> None:
        with self.conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO releases
                   (id, title, edition, studio, year, release_date, url, first_seen, digest_sent)
                   VALUES (:id, :title, :edition, :studio, :year, :release_date, :url, :first_seen, :digest_sent)""",
                {**item, "first_seen": now_iso(), "digest_sent": int(digest_sent)},
            )

    def update_listing(self, item: dict) -> None:
        """Refresh fields the search listing knows about (release dates move)."""
        with self.conn() as c:
            c.execute(
                """UPDATE releases SET title=:title, edition=:edition, studio=:studio,
                   year=:year, release_date=COALESCE(:release_date, release_date)
                   WHERE id=:id""",
                item,
            )

    def apply_product(self, rid: int, product: dict) -> None:
        with self.conn() as c:
            c.execute(
                """UPDATE releases SET is_steelbook=:is_steelbook, preorder=:preorder,
                   price=:price, availability=:availability, buy_url=:buy_url, cover=COALESCE(:cover, cover),
                   release_date=COALESCE(:release_date, release_date), last_checked=:now,
                   preorder_seen_at=CASE WHEN :preorder=1 AND preorder_seen_at IS NULL
                                         THEN :now ELSE preorder_seen_at END
                   WHERE id=:id""",
                {**product, "id": rid, "now": now_iso()},
            )

    def releases_needing_check(self, recheck_before: str, today: str, limit: int) -> list[sqlite3.Row]:
        """Unchecked rows first, then upcoming steelbooks not yet on preorder."""
        with self.conn() as c:
            return c.execute(
                """SELECT * FROM releases
                   WHERE hidden=0 AND purchased=0 AND (
                       last_checked IS NULL OR (
                           is_steelbook=1 AND preorder=0
                           AND (release_date IS NULL OR release_date >= ?)
                           AND last_checked < ?))
                   ORDER BY last_checked IS NOT NULL, last_checked
                   LIMIT ?""",
                (today, recheck_before, limit),
            ).fetchall()

    def pending_preorder_alerts(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                """SELECT * FROM releases WHERE is_steelbook=1 AND preorder=1
                   AND preorder_notified=0 AND hidden=0 AND purchased=0"""
            ).fetchall()

    def mark_preorder_notified(self, ids: list[int]) -> None:
        with self.conn() as c:
            c.executemany("UPDATE releases SET preorder_notified=1 WHERE id=?", [(i,) for i in ids])

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
            "upcoming": "hidden=0 AND purchased=0 AND (release_date IS NULL OR release_date >= :today)",
            "preorder": "hidden=0 AND purchased=0 AND preorder=1",
            "purchased": "purchased=1",
            "hidden": "hidden=1",
            "all": "1=1",
        }.get(view, "hidden=0 AND purchased=0")
        with self.conn() as c:
            return c.execute(
                f"""SELECT * FROM releases WHERE is_steelbook=1 AND {where}
                    ORDER BY release_date IS NULL, release_date, title""",
                {"today": today},
            ).fetchall()

    def counts(self, today: str) -> dict:
        with self.conn() as c:
            row = c.execute(
                """SELECT
                     SUM(hidden=0 AND purchased=0 AND (release_date IS NULL OR release_date >= :t)) AS upcoming,
                     SUM(hidden=0 AND purchased=0 AND preorder=1) AS preorder,
                     SUM(purchased=1) AS purchased,
                     SUM(hidden=1) AS hidden,
                     COUNT(*) AS total
                   FROM releases WHERE is_steelbook=1""",
                {"t": today},
            ).fetchone()
            return {k: (row[k] or 0) for k in row.keys()}

    # --- community --------------------------------------------------------
    def insert_community(self, post: dict, *, digest_sent: bool) -> bool:
        with self.conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO community(id, title, url, published, first_seen, digest_sent)
                   VALUES (:id, :title, :url, :published, :first_seen, :digest_sent)""",
                {**post, "first_seen": now_iso(), "digest_sent": int(digest_sent)},
            )
            return cur.rowcount > 0

    def pending_community(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM community WHERE digest_sent=0 ORDER BY published").fetchall()

    def mark_community_sent(self, ids: list[str]) -> None:
        with self.conn() as c:
            c.executemany("UPDATE community SET digest_sent=1 WHERE id=?", [(i,) for i in ids])

    def recent_community(self, limit: int = 15) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM community ORDER BY published DESC LIMIT ?", (limit,)
            ).fetchall()

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
