"""Home Assistant webhook notifications.

Every event is POSTed as JSON to HA_WEBHOOK_URL. A Home Assistant automation
(see homeassistant/automation.yaml) turns it into a phone notification.
"""

import logging

import httpx

log = logging.getLogger(__name__)


async def send(webhook_url: str, payload: dict) -> bool:
    if not webhook_url:
        log.info("HA_WEBHOOK_URL not set; would have sent: %s", payload.get("title"))
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(webhook_url, json=payload)
            r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.error("Home Assistant webhook failed: %s", e)
        return False


def preorder_payload(row, app_url: str | None = None) -> dict:
    when = row["release_date"] or "date TBA"
    price = f" · {row['price']}" if row["price"] else ""
    return {
        "type": "preorder",
        "title": "Steelbook preorder live",
        "message": f"{row['title']}{_edition(row)} ({when}){price}",
        "url": row["buy_url"] or row["url"],
        "image": row["cover"],
        "id": row["id"],
    }


def digest_payload(releases, community, app_url: str | None = None) -> dict:
    lines = []
    for r in releases:
        lines.append(f"• {r['title']}{_edition(r)} ({r['release_date'] or 'TBA'})")
    if community:
        lines.append("")
        lines.append("From r/steelbooks:")
        lines.extend(f"• {c['title']}" for c in community)
    n = len(releases)
    title = f"{n} new steelbook{'s' if n != 1 else ''} announced" if n else "Steelbook community news"
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
