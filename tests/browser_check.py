"""Run inside the Docker image by CI: can the image's own Chromium read the shops?

    docker run --rm -v "$PWD/tests:/app/tests:ro" <image> python -m tests.browser_check

The unit tests run a Chromium on the CI machine. This runs the one in the image, as
root and with Docker's defaults, the way the container runs on Unraid: the tracker in
SHOP_READER=browser mode reads three stand-in shops on localhost twice, keeping its
profile under DATA_DIR in between. Only what the image holds is used (no pytest).
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

os.environ["SHOP_READER"] = "browser"
os.environ["RUN_ON_START"] = "false"

from app import browser, config, shops  # noqa: E402
from app.db import DB  # noqa: E402
from app.jobs import Tracker  # noqa: E402
from tests.shopfront import Shopfront  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def browser_processes() -> list[str]:
    """Chromium's processes that are still there, finished-but-not-cleared-away ones included."""
    found = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                name = (proc / "comm").read_text().strip()
                state = (proc / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except OSError:
                continue
            if "chrom" in name or "headless" in name:
                found.append(f"{name} ({'zombie' if state == 'Z' else state})")
    return found


async def main() -> None:
    lists = {shop.feed: json.loads((FIX / f"shop_{shop.key}.json").read_text(encoding="utf-8"))["products"]
             for shop in shops.SHOPS}
    browser.SHOP_GAP = (1, 2)
    shops.MIN_GAP = 0
    with Shopfront(lists) as front:
        shops.SHOPS = tuple(replace(shop, site=front.url) for shop in shops.SHOPS)
        settings = config.load()
        assert settings.shop_reader == "browser" and str(settings.data_dir) == "/data", settings
        tracker = Tracker(settings, DB(settings.db_path))
        for expected_visitors in (1, 1):  # the second read comes back with the first one's cookie
            result = await tracker.run_shops()
            print(result)
            assert result.startswith("Shops: 3 of 3 read"), result
            assert front.visitors == expected_visitors, front.visitors
        agent = front.last(shops.SHOPS[0].page).headers["user-agent"]
        print("The shops saw:", agent)
        assert "HeadlessChrome" in agent, "the browser must not pass itself off as something else"
        assert front.last("/seen").query == {"webdriver": ["true"]}, front.last("/seen").query
        assert front.last(shops.SHOPS[0].page).headers.get("cookie") == "visitor=1"
    assert (settings.data_dir / browser.PROFILE / "Default").is_dir(), "no profile was kept under /data"
    # Closed, and nothing left behind: tini (process 1) clears away what Chromium orphans.
    for _ in range(20):
        left = browser_processes()
        if not left:
            break
        time.sleep(0.5)
    assert not left, f"browser processes were left behind: {left}"
    print("Browser check passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        sys.exit(f"Browser check FAILED: {e}")
