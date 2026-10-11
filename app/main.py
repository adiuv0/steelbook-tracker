"""Web UI, JSON API and the background scheduler."""

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import config
from .db import DB
from .jobs import Tracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("steelbook")

settings = config.load()
db = DB(settings.db_path)
tracker = Tracker(settings, db)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _seconds_until(hhmm: str) -> float:
    try:
        hour, minute = (int(x) for x in hhmm.split(":"))
    except ValueError:
        hour, minute = 9, 0
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def scan_loop():
    if settings.run_on_start:
        await asyncio.sleep(5)
        await tracker.run_scan()
    while True:
        # Jitter so requests don't land on the same minute every time.
        await asyncio.sleep(settings.poll_hours * 3600 + random.uniform(-600, 600))
        await tracker.run_scan()


async def digest_loop():
    while True:
        await asyncio.sleep(_seconds_until(settings.digest_time))
        try:
            log.info(await tracker.send_digest())
        except Exception:
            log.exception("digest failed")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(scan_loop()), asyncio.create_task(digest_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Steelbook Tracker", lifespan=lifespan)

VIEWS = [
    ("upcoming", "Upcoming"),
    ("preorder", "Preorder live"),
    ("purchased", "Purchased"),
    ("hidden", "Hidden"),
    ("all", "All"),
]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, view: str = "upcoming"):
    today = date.today().isoformat()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "view": view,
            "views": VIEWS,
            "releases": db.list_releases(view, today),
            "counts": db.counts(today),
            "health": db.health(),
            "community": db.recent_community(),
            "last_run": tracker.last_run,
            "last_result": tracker.last_result,
            "scanning": tracker.lock.locked(),
            "today": today,
        },
    )


class Flag(BaseModel):
    value: bool


@app.post("/api/releases/{rid}/{flag}")
async def set_flag(rid: int, flag: str, body: Flag):
    if flag not in {"purchased", "hidden"}:
        raise HTTPException(404)
    if db.get_release(rid) is None:
        raise HTTPException(404)
    db.set_flag(rid, flag, body.value)
    return {"id": rid, flag: body.value}


@app.get("/api/releases")
async def api_releases(view: str = "upcoming"):
    rows = db.list_releases(view, date.today().isoformat())
    return [dict(r) for r in rows]


@app.get("/api/summary")
async def api_summary():
    """Small payload for Home Assistant REST sensors."""
    today = date.today().isoformat()
    upcoming = db.list_releases("upcoming", today)
    next_up = next((r for r in upcoming if r["release_date"]), None)
    return {
        **db.counts(today),
        "next_title": next_up["title"] if next_up else None,
        "next_date": next_up["release_date"] if next_up else None,
        "last_run": tracker.last_run,
        "sources_failing": sum(1 for h in db.health() if h["fail_count"] > 0),
    }


@app.post("/api/scan")
async def api_scan():
    if tracker.lock.locked():
        return JSONResponse({"status": "already running"}, status_code=409)
    asyncio.create_task(tracker.run_scan())
    return {"status": "started"}


@app.post("/api/digest")
async def api_digest():
    return {"status": await tracker.send_digest(force=True)}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
