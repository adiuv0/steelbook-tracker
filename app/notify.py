"""Home Assistant webhook notifications.

Every event is POSTed as JSON to HA_WEBHOOK_URL. A Home Assistant automation
(see homeassistant/automation.yaml) turns it into a phone notification.
"""

import httpx


async def send(webhook_url: str, payload: dict) -> None:
    """POST one event to the webhook. Raises if it does not go through."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(webhook_url, json=payload)
        r.raise_for_status()


def preorder_payload(row) -> dict:
    when = row["release_date"] or "date TBA"
    price = f" · {row['price']}" if row["price"] else ""
    return {
        "type": "preorder",
        "title": "Steelbook preorder live",
        "message": f"{row['title']}{_edition(row)} ({when}){price}{_store(row)}",
        "url": row["buy_url"] or row["url"],
        "image": row["cover"],
        "id": row["id"],
    }


def restock_payload(row) -> dict:
    """A title that could not be ordered can be again (back on preorder or in stock)."""
    status = f" · {row['availability']}" if row["availability"] else ""
    price = f" · {row['price']}" if row["price"] else ""
    return {
        "type": "restock",
        "title": "Steelbook available to order",
        "message": f"{row['title']}{_edition(row)}{status}{price}{_store(row)}",
        "url": row["buy_url"] or row["url"],
        "image": row["cover"],
        "id": row["id"],
    }


# A push notification is no place for a long list; the rest is on the page or at the shop.
MAX_SHOP_LINES = 8
MAX_HEADLINES = 8


def shop_payload(shop, kind: str, rows) -> dict:
    """Everything of one kind that changed at one shop since the last look, as one alert.

    ``kind`` is 'new' (a listing not seen before that can be ordered) or 'restock'
    (one that could not be ordered and now can). They are sent as the ``preorder``
    and ``restock`` types so Home Assistant treats them like the Blu-ray.com alerts.
    """
    n = len(rows)
    if kind == "new":
        title = f"New 4K steelbook at {shop.name}" if n == 1 else f"{n} new 4K steelbooks at {shop.name}"
    else:
        title = (f"Available to order at {shop.name}" if n == 1
                 else f"{n} steelbooks available to order at {shop.name}")

    def line(row) -> str:
        studio = f" · {row['studio']}" if row["studio"] else ""
        price = f" · {row['price']}" if row["price"] else ""
        return f"{row['title']}{studio}{price}{' · pre-order' if row['preorder'] else ''}"

    if n == 1:
        message = line(rows[0])
    else:
        lines = [f"• {line(r)}" for r in rows[:MAX_SHOP_LINES]]
        if n > MAX_SHOP_LINES:
            lines.append(f"and {n - MAX_SHOP_LINES} more")
        message = "\n".join(lines)
    return {
        "type": "preorder" if kind == "new" else "restock",
        "title": title,
        "message": message,
        "url": rows[0]["url"] if n == 1 else shop.page_url,
        "image": rows[0]["image"] if n == 1 else None,
        "id": f"{shop.key}-{rows[0]['id']}",
        "store": shop.name,
    }


def digest_payload(releases, headlines=(), app_url: str | None = None) -> dict:
    lines = []
    for r in releases:
        lines.append(f"• {r['title']}{_edition(r)} ({r['release_date'] or 'TBA'}){_store(r)}")
    n = len(releases)
    if headlines:
        if lines:
            lines.append("")
            lines.append("In the news:")
        lines.extend(f"• {h['title']}" for h in headlines[:MAX_HEADLINES])
        if len(headlines) > MAX_HEADLINES:
            lines.append(f"and {len(headlines) - MAX_HEADLINES} more headlines on the tracker page")
    if n:
        title = f"{n} new steelbook{'s' if n != 1 else ''} announced"
    elif headlines:
        title = "Steelbook news"
    else:  # only when a digest is asked for by hand
        title = "Steelbook digest"
        lines.append("Nothing new since the last digest.")
    return {
        "type": "digest",
        "title": title,
        "message": "\n".join(lines),
        "url": app_url or "",
        "count": n,
    }


def health_payload(source: str, error: str, recovered: bool = False) -> dict:
    if recovered:
        return {"type": "health", "title": "Steelbook tracker recovered",
                "message": f"{source} is returning data again."}
    return {
        "type": "health",
        "title": "Steelbook tracker source failing",
        "message": f"{source} has failed repeatedly: {error[:200]}",
    }


def _edition(row) -> str:
    return f" ({row['edition']})" if row["edition"] else ""


def _store(row) -> str:
    return f" · {row['exclusive']} exclusive" if row["exclusive"] else ""
