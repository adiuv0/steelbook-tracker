# Steelbook Tracker

A small self-hosted service that watches for newly announced US 4K steelbooks, tells you when preorders open, and gives you a web page to tick off what you've bought. It runs as one Docker container (built for Unraid) and sends alerts through Home Assistant.

## What it does

Every few hours (default 4) it:

1. Reads Blu-ray.com's search for "4k steelbook", sorted by newest additions and by newest preorders. Anything with an upcoming release date is stored.
2. Opens each new product page to confirm it really is a 4K SteelBook and to see whether it is "Available for pre-order". Newest listings are checked first, and upcoming steelbooks that aren't on preorder yet are rechecked every 12 hours. Listings that haven't been checked yet still show in the web UI, marked "Not verified yet". If a scan is cut short (Blu-ray.com stops answering for a bit), it pauses, reconnects and carries on, and anything left over is picked up by a follow-up scan 20 minutes later instead of at the next 4-hour scan.
3. Reads the r/steelbooks RSS feed and keeps posts that look like drops, restocks or preorders (hauls and shelf photos are filtered out).

Notifications go to a Home Assistant webhook:

| When | What you get |
| --- | --- |
| A tracked steelbook's preorder goes live | Instant, high-priority push with price and a buy link |
| Daily at `DIGEST_TIME` | One summary of newly announced steelbooks plus relevant r/steelbooks posts |
| A source fails 3 runs in a row | One "source failing" push, and one "recovered" push when it comes back |

Anything you mark **Purchased** or **Hide** in the web UI stops generating alerts.

The very first run is a quiet seed: it records everything already announced, then sends a single "tracker is running" message instead of dozens of alerts.

### Why only these sources

Tested from a home connection in October 2026: Walmart serves a captcha, Amazon returns 503, Best Buy no longer sells discs, and HiDefNinja and Trackalacker sit behind Cloudflare. Blu-ray.com lists retailer exclusives (Walmart, Target, etc.) as separate editions, so those still show up there; r/steelbooks catches drops people post about.

Blu-ray.com only answers scripted requests that behave like a browser session (it returns the text `error42` otherwise). The client opens the home page first to get a session cookie, waits 4–9 seconds between requests, and re-warms the session once if it gets blocked. If Blu-ray.com changes its layout the search will come back empty and you'll get a "source failing" alert.

## Install on Unraid

1. The GitHub Action publishes the image on every push to `main`, to Docker Hub as `adiuv0/steelbook-tracker:latest` (the one the Unraid template uses) and to `ghcr.io/adiuv0/steelbook-tracker:latest`. Docker Hub publishing needs a repository secret named `DOCKERHUB_TOKEN`: create an access token with Read & Write scope at Docker Hub → Account settings → Personal access tokens, then add it under GitHub → steelbook-tracker → Settings → Secrets and variables → Actions. Without it the build still succeeds and only pushes to GHCR.
2. Copy `unraid/steelbook-tracker.xml` to `/boot/config/plugins/dockerMan/templates-user/` on the Unraid flash drive.
3. Docker tab → Add Container → pick **steelbook-tracker** from the template list.
4. Fill in:
   - **Home Assistant webhook URL**: `http://<home-assistant-ip>:8123/api/webhook/<webhook id>` (the webhook id is in the "Steelbook tracker notifications" automation's trigger).
   - **App URL**: `http://<unraid-ip>:8787`, so notifications can link back to the page.
5. Apply. The web UI is at `http://<unraid-ip>:8787`, and clicking the container's icon on the Docker tab offers **WebUI** to open it. The first scan starts a few seconds after the container does and takes 10–20 minutes because it is deliberately slow.

The template sets the icon and the WebUI link, and the image carries the same values as `net.unraid.docker.icon` / `net.unraid.docker.webui` labels. A container that was added before the template had them keeps its own saved copy of the template, so if the icon or WebUI entry is missing, edit the container, switch to **Advanced View**, and fill in:

- **Icon URL**: `https://raw.githubusercontent.com/adiuv0/steelbook-tracker/main/app/static/icon.png`
- **WebUI**: `http://[IP]:[PORT:8787]/`

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `HA_WEBHOOK_URL` | (empty) | Home Assistant webhook. Empty = log only. |
| `APP_URL` | (empty) | Link to this UI, used in the digest notification. |
| `TZ` | `America/Los_Angeles` | Timezone for the digest time. |
| `DIGEST_TIME` | `09:00` | When the daily summary is sent. |
| `POLL_HOURS` | `4` | Hours between scans (±10 minutes of jitter). |
| `RECHECK_HOURS` | `12` | How often an upcoming steelbook's page is rechecked for a preorder. |
| `MAX_PAGE_FETCHES` | `40` | Product pages fetched per scan (5× on the first run). |
| `SCAN_PAGES` / `SEED_PAGES` | `2` / `8` | Search pages read per scan / on the first run (20 results per page). |
| `REQUEST_DELAY_MIN` / `MAX` | `4` / `9` | Seconds to wait between Blu-ray.com requests. |
| `HEALTH_FAIL_THRESHOLD` | `3` | Consecutive failures before a "source failing" alert. |
| `FAILURE_BACKOFF_SECONDS` | `60` | Pause after a failed product page before reconnecting (doubles on the next failure). |
| `BACKLOG_RETRY_MINUTES` | `20` | When listings are still unchecked after a scan, run the next scan this soon. |
| `REDDIT_ENABLED` | `true` | Include r/steelbooks posts. |

Data lives in `/data/tracker.db` (SQLite). Back it up by copying the file.

## API

- `GET /api/releases?view=upcoming|preorder|purchased|hidden|all` – JSON list.
- `GET /api/summary` – counts and the next release, handy for a Home Assistant REST sensor.
- `POST /api/releases/{id}/purchased` or `/hidden` with `{"value": true}`.
- `POST /api/scan` – start a scan now. `POST /api/digest` – send the digest now.

## Home Assistant

`homeassistant/automation.yaml` is a reference copy of the automation that turns webhook calls into phone notifications. Preorder alerts use their own Android notification channel ("Steelbook preorders") so you can give them a louder sound in the Companion app.

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
DATA_DIR=./data uvicorn app.main:app --reload --port 8787
```
