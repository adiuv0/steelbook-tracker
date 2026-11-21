# Steelbook Tracker

A small self-hosted service that watches for newly announced US 4K steelbooks, tells you when preorders open or a shop has one in stock, and gives you a web page to tick off what you've bought. It runs as one Docker container (built for Unraid) and sends alerts through Home Assistant.

## What it does

Every few hours (default 4) it:

1. Reads Blu-ray.com's search for "4k steelbook", sorted by newest additions and by newest preorders. Anything with an upcoming release date is stored.
2. Opens each new product page to confirm it really is a 4K SteelBook and to see whether it is "Available for pre-order". Newest listings are checked first. Until a title is released its page keeps being rechecked: every 12 hours while it isn't on preorder, once a day after that (prices, release dates and availability still move), and straight away when it turns up in Blu-ray.com's newest-preorders list, so a preorder opening is normally caught by the next scan. A title that can't be ordered ("Currently unavailable") is looked at on every scan instead, and for 30 days past its release, so you hear when it can be ordered again. Listings that haven't been checked yet still show in the web UI, marked "Not verified yet". If a scan is cut short (Blu-ray.com stops answering for a bit), it pauses, reconnects and carries on, and anything left over is picked up by a follow-up scan 20 minutes later instead of at the next 4-hour scan. Follow-ups that keep getting nowhere back off (40 minutes, 80, and so on, up to the normal interval).
3. Reads HD Report's steelbook headlines, for the daily digest.

Every hour (default) it also reads the 4K steelbooks that [three shops](#the-shops) list themselves, and whether each can be ordered.

Notifications go to a Home Assistant webhook:

| When | What you get |
| --- | --- |
| A tracked steelbook's preorder goes live | Instant, high-priority push with price and a buy link |
| A steelbook that couldn't be ordered can be again (back on preorder or in stock) | Instant, high-priority push with the new status, price and a buy link |
| A watched shop lists a new 4K steelbook that can be ordered | Instant, high-priority push with the price and a link to the shop. Several new titles at one shop arrive as one push. |
| A 4K steelbook at a watched shop couldn't be ordered and now can | Instant, high-priority push, at most once a day per title |
| Daily at `DIGEST_TIME` | One summary of newly announced steelbooks and new steelbook headlines |
| A source fails 3 runs in a row | One "source failing" push, and one "recovered" push when it comes back |

Anything you mark **Purchased** or **Hide** in the web UI stops generating alerts from Blu-ray.com. The shops' alerts are separate: a shop's listing isn't matched to a card, so marking a title doesn't silence a restock of it at a shop.

If Home Assistant can't be reached, the alert is kept and sent on a later scan, and the failure is listed under **Sources** in the web UI. With no webhook set at all, alerts are only written to the container log and the page says notifications are off.

The very first run is a quiet seed: it records everything already announced, then sends a single "tracker is running" message instead of dozens of alerts.

### Store exclusives, and what can't be alerted on

Blu-ray.com names the store in a product's title ("Amazon Exclusive SteelBook", "Wal-Mart Exclusive SteelBook"). The tracker shows it as a badge on the card and adds it to alerts and the digest.

The only stock Blu-ray.com reports is Amazon's. Its preorder and restock alerts are therefore only possible for titles Amazon sells. An edition that is exclusive to Walmart, Target or another store the tracker can't read is still announced in the daily digest and listed with its store, but the tracker cannot tell when it goes on sale, and its card says so. An exclusive of a [watched shop](#the-shops) is covered from the shop's side instead.

Restocks are noticed at the next scan, so with the default settings an alert can be up to four hours behind. That catches a restock that lasts; one that sells out again within minutes will be missed. Lower `POLL_HOURS` to look more often.

### The shops

Three shops publish their catalogue in a form a script may read, with the shop's own "can be ordered" flag on every product:

| Shop | What is read | Setting name |
| --- | --- | --- |
| [Gruv](https://www.gruv.com/collections/4k-steelbooks) | Its "4K Steelbooks" collection, about 240 titles from Universal, Warner Bros., Shout! Factory, AMC and others | `gruv` |
| [Lionsgate Limited](https://www.lionsgatelimited.com) | The whole shop, about 70 of whose products are 4K steelbooks | `lionsgate` |
| [MVD Shop](https://mvdshop.com/collections/steelbooks) | Its "SteelBooks" collection, a handful of titles | `mvd` |

Each is one request per check. The first read of a shop is quiet: everything it already sells is recorded and nothing is sent. After that:

- A product that wasn't there before and can be ordered is a **new** alert.
- A product that couldn't be ordered and now can is an **available to order** alert. That covers a restock, and a listing that went up before it went on sale. Stock that flickers in and out produces one alert a day per title, not one each time.
- Alerts are grouped: ten new titles at one shop are one push with the first eight named.

What can be ordered right now is listed under **At the shops** on the web page, newest first, with a count per shop.

Things to know:

- Shop listings are not matched to the cards from Blu-ray.com. The same film can appear in both places, and **Purchased** / **Hide** on a card doesn't stop a shop alert for it.
- A hot title can sell out between two checks. Lower `SHOP_POLL_MINUTES` (minimum 10, or 60 when the shops are read with a browser) to look more often, but read the next point first.
- All three shops run on Shopify, which limits automated readers, and since May 2026 limits most strictly the ones that don't sign their requests. The tracker doesn't. A reader Shopify has had enough of is answered "too many requests", and it stays refused for a good while if it keeps asking. So the tracker asks for little: one request per shop per read, the shops a few seconds apart, and never the same shop twice within five minutes. When a shop says "too many requests" the read stops there and no shop is asked for half an hour (the shops refuse together, so asking the next one only adds to the tally). If the next read is refused too the wait doubles, and again, up to four hours, so while Shopify is refusing the tracker it gets one request per wait instead of a steady stream. Meanwhile the shops show as failing under **Sources**, keep the listings they had, and the **At the shops** panel says until when they are being left alone. Three refused reads in a row send the usual "source failing" alert. These rules hold whatever starts a read: the timer, a restart of the container, or **Scan now**. How long Shopify keeps refusing is up to Shopify; on 2026-11-21 it was still refusing an hour and a half after the first refusal. Asking more often can only make the limit bite more. Out of the box the tracker does not use a browser to get round it; that is a separate setting you switch on yourself: see [Reading the shops with a browser](#reading-the-shops-with-a-browser).
- **Scan now** (top right of the page) scans Blu-ray.com and reads the shops again. Pressing it repeatedly does not ask the shops repeatedly: a shop that was asked in the last five minutes is skipped, so are all of them while they are being left alone, and the panel says so.
- Leave a shop out of `SHOPS` to stop watching it, or set `SHOPS=none` to switch the feature off.

#### Reading the shops with a browser

Out of the box the tracker asks each shop for its product list directly, as the program it is, and Shopify turns that kind of reader away for long stretches (the point about Shopify above). `SHOP_READER=browser` reads the shops the way you would look yourself. A real browser inside the container (Chromium, in the build that [Playwright](https://playwright.dev) installs) opens each shop's page, with everything on it, in a profile that keeps its cookies from one visit to the next (`/data/browser`). The page then asks its own shop for the product list, the way a storefront's scripts ask for things, and the tracker reads the answer. One shop at a time, 15 to 45 seconds apart, about once an hour: `SHOP_POLL_MINUTES`, give or take five minutes, and never less than 60 with this reader.

Read this before you switch it on:

- **It is still a bot, and it doesn't hide that.** To Shopify an automated browser is a bot whatever it looks like. This one runs as it comes: its user agent says "HeadlessChrome" and it reports that it is automated, so a shop that wants to know can tell. No stealth plug-ins, no altered fingerprint, no proxies, and it never answers a check meant for people. That is deliberate, and there is no setting for it.
- **It can be refused too.** If a shop answers "too many requests", shows a check for people (a captcha, a "verify you are human" page) or won't serve the browser, the read stops there, the shops show as failing, and no shop is asked for half an hour. Each refusal in a row doubles that, up to a day. Nothing is tried again sooner, or another way.
- **The risk is to your own browsing.** A refusal of the plain reader sticks to the tracker's kind of connection, which is why your own browser is served as usual while the tracker is turned away. A browser on your home address looks much more like you. If Shopify takes against it, you may start seeing checks yourself at these shops from that address.
- **It is heavier.** Chromium makes the image several hundred megabytes bigger, and a read takes a few hundred megabytes of memory for a minute or two. The browser only runs while the shops are being read.

To switch it on in Unraid: Docker tab → the container's icon → **Edit** → set **How the shops are read** to `browser` → **Apply**. A container added before that field existed doesn't show it: use **Add another Path, Port, Variable, Label or Device** → Config Type **Variable**, Key `SHOP_READER`, Value `browser`. The **At the shops** panel then says the shops are read by opening each shop's page in a browser. A change of reader starts with a clean slate: a wait the other reader earned is dropped. Set it to `feed`, or remove the variable, to go back.

### Why these sources

Tested from a home connection in October and November 2026: Walmart serves a captcha, Amazon returns 503, Best Buy no longer sells discs, and HiDefNinja and Trackalacker sit behind Cloudflare. Shout! Factory's and Kino Lorber's own shops, Home Theater Forum and The Digital Bits refuse scripted requests too. Blu-ray.com lists retailer exclusives (Walmart, Target, etc.) as separate editions, so those still show up there.

Nothing reads Walmart's stock. The nearest thing is [HD Report](https://hd-report.com/tag/steelbook/), which tags its steelbook posts (preorders opening, artwork reveals, sell-outs, deals at any store) and publishes them as an RSS feed, about twenty posts a month. The tracker keeps the headline and the link, shows the latest on the web page, and adds new ones to the daily digest. Set `NEWS_ENABLED=false` to leave them out.

Until November 2026 the tracker also read the r/steelbooks RSS feed for drop and restock posts. Reddit ended RSS support on 13 November 2026 with no replacement for feeds like this one, and is ending public API access by March 2027, so that source was removed.

Blu-ray.com only answers scripted requests that behave like a browser session (it returns the text `error42` otherwise). The client opens the home page first to get a session cookie, waits 4–9 seconds between requests, and re-warms the session once if it gets blocked. If Blu-ray.com changes its layout, the search comes back empty or product pages stop being recognised; either way you get a "source failing" alert instead of wrong data.

## Install on Unraid

1. The GitHub Action publishes the image on every push to `main`, to Docker Hub as `adiuv0/steelbook-tracker:latest` (the one the Unraid template uses) and to `ghcr.io/adiuv0/steelbook-tracker:latest`. Docker Hub publishing needs a repository secret named `DOCKERHUB_TOKEN`: create an access token with Read & Write scope at Docker Hub → Account settings → Personal access tokens, then add it under GitHub → steelbook-tracker → Settings → Secrets and variables → Actions. Without it the build still succeeds and only pushes to GHCR.
2. Copy `unraid/steelbook-tracker.xml` to `/boot/config/plugins/dockerMan/templates-user/` on the Unraid flash drive. From the Unraid terminal that is one line:
   ```
   wget -O /boot/config/plugins/dockerMan/templates-user/my-steelbook-tracker.xml https://raw.githubusercontent.com/adiuv0/steelbook-tracker/main/unraid/steelbook-tracker.xml
   ```
3. Docker tab → Add Container → pick **steelbook-tracker** from the template list. Don't add the container by typing the image name into a blank form: that leaves out the data folder, so the first update would start again from an empty database (see [Keeping your data](#keeping-your-data)).
4. Fill in:
   - **Home Assistant webhook URL**: `http://<home-assistant-ip>:8123/api/webhook/<webhook id>` (the webhook id is in the "Steelbook tracker notifications" automation's trigger).
   - **App URL**: `http://<unraid-ip>:8787`, so notifications can link back to the page.
5. Apply. The web UI is at `http://<unraid-ip>:8787`, and clicking the container's icon on the Docker tab offers **WebUI** to open it. The first scan starts a few seconds after the container does and takes 10–20 minutes because it is deliberately slow.

The template sets the icon and the WebUI link, and the image carries the same values as `net.unraid.docker.icon` / `net.unraid.docker.webui` labels. A container that was added before the template had them keeps its own saved copy of the template, so if the icon or WebUI entry is missing, edit the container, switch to **Advanced View**, and fill in:

- **Icon URL**: `https://raw.githubusercontent.com/adiuv0/steelbook-tracker/main/app/static/icon.png`
- **WebUI**: `http://[IP]:[PORT:8787]/`

## Keeping your data

Everything the tracker knows, including what you marked **Purchased** or **Hide**, is one file: `/data/tracker.db` inside the container. Updating a container replaces it, so that file only survives when `/data` is mapped to a folder on the server. The template maps it to `/mnt/user/appdata/steelbook-tracker`.

A container added without the template has no such mapping. Docker then keeps `/data` in a throwaway volume: everything works until the first update, which starts again from an empty database. The tracker checks for this when it starts. If the data folder isn't mapped, the web page shows a red warning at the top, the container log says `DATA IS NOT BEING KEPT`, and `/api/summary` reports `"data_persistent": false`.

To fix a container that is already running without losing what's in it, **copy the database out before you change or update anything**. In the Unraid terminal (use your container's name if it isn't `steelbook-tracker`):

```
docker stop steelbook-tracker
mkdir -p /mnt/user/appdata/steelbook-tracker
docker cp steelbook-tracker:/data/tracker.db /mnt/user/appdata/steelbook-tracker/
```

Then on the Docker tab click the container's icon → **Edit** → **Add another Path, Port, Variable, Label or Device** and add:

- Config Type **Path**, Container Path `/data`, Host Path `/mnt/user/appdata/steelbook-tracker`, Access Mode Read/Write.

Click **Apply**. Unraid re-creates the container on top of the copied database, and the warning is gone. If the same container is also missing the **Home Assistant webhook URL** and **App URL** fields, add them in the same edit as Config Type **Variable** with keys `HA_WEBHOOK_URL` and `APP_URL`.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `HA_WEBHOOK_URL` | (empty) | Home Assistant webhook. Empty = log only. |
| `APP_URL` | (empty) | Link to this UI, used in the digest notification. |
| `TZ` | `America/Los_Angeles` | Timezone for the digest time. |
| `DIGEST_TIME` | `09:00` | When the daily summary is sent. |
| `POLL_HOURS` | `4` | Hours between scans (±10 minutes of jitter). |
| `RECHECK_HOURS` | `12` | How often an upcoming steelbook's page is rechecked for a preorder. |
| `PREORDER_RECHECK_HOURS` | `24` | How often a steelbook that is on preorder is rechecked (price, date, sold out) until it is released. |
| `RESTOCK_WATCH_DAYS` | `30` | How long after its release a title that can't be ordered is still watched for a restock. `0` stops watching at release. |
| `MAX_PAGE_FETCHES` | `40` | Product pages fetched per scan (5× on the first run). |
| `SCAN_PAGES` / `SEED_PAGES` | `2` / `8` | Search pages read per scan / on the first run (20 results per page). |
| `REQUEST_DELAY_MIN` / `REQUEST_DELAY_MAX` | `4` / `9` | Seconds to wait between Blu-ray.com requests. |
| `HEALTH_FAIL_THRESHOLD` | `3` | Consecutive failures before a "source failing" alert. |
| `FAILURE_BACKOFF_SECONDS` | `60` | Pause after a failed product page before reconnecting (doubles on the next failure). |
| `BACKLOG_RETRY_MINUTES` | `20` | When listings are still unchecked after a scan, run the next scan this soon (doubling while follow-ups make no progress). |
| `SHOPS` | `gruv,lionsgate,mvd` | Which [shops](#the-shops) to watch. `none` watches no shop. |
| `SHOP_POLL_MINUTES` | `60` | Minutes between reads of the shops (minimum 10; minimum 60 with `SHOP_READER=browser`). |
| `SHOP_READER` | `feed` | How the shops are read. `feed` asks each shop for its product list directly. `browser` opens each shop's page in Chromium instead: read [Reading the shops with a browser](#reading-the-shops-with-a-browser) first. |
| `NEWS_ENABLED` | `true` | Read HD Report's steelbook headlines for the digest and the web page. |
| `RUN_ON_START` | `true` | Scan a few seconds after the container starts. |
| `DATA_DIR` | `/data` | Where the database is kept. |

Data lives in `/data/tracker.db` (SQLite). Back it up by copying the file. (`/data/browser`, if it exists, is the profile of the [browser reader](#reading-the-shops-with-a-browser): cookies and cache, safe to delete while the container is stopped.) It has to be mapped to a folder on the server to survive updates: see [Keeping your data](#keeping-your-data).

## API

- `GET /api/releases?view=upcoming|preorder|purchased|hidden|all` – JSON list.
- `GET /api/summary` – counts, the next release, whether a scan is running (`scanning`) or the shops are being read (`shops_checking`), when the shops were last read, how they are read (`shop_reader`: `feed` or `browser`), until when they are being left alone after a "too many requests" or a check for people (`shops_left_alone_until`, otherwise `null`), how many sources are failing, and `data_persistent` (`false` when the database would not survive a container update); handy for a Home Assistant REST sensor.
- `POST /api/releases/{id}/purchased` or `/hidden` with `{"value": true}`.
- `POST /api/scan` – start a scan now (Blu-ray.com and the shops). `POST /api/digest` – send the digest now.

## Home Assistant

`homeassistant/automation.yaml` is a reference copy of the automation that turns webhook calls into phone notifications. Preorder and restock alerts use their own Android notification channel ("Steelbook preorders") so you can give them a louder sound in the Companion app. Each webhook call carries a `type`: `preorder`, `restock`, `digest`, `health` or `setup`. Alerts from the shops use `preorder` (a new listing) and `restock` too, so they arrive on the same channel, and carry the shop's name in `store`.

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium   # for the browser reader and its tests
pytest
DATA_DIR=./data uvicorn app.main:app --reload --port 8787
```

The browser reader's tests start a real Chromium against a stand-in shop on localhost. Without a Chromium they are skipped (set `REQUIRE_BROWSER=1`, as CI does, to make that a failure). `BROWSER_PATH` points the reader at another Chromium than the one Playwright installs.
