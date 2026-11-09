from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def client():
    return TestClient(main.app)  # no lifespan: the scheduler doesn't start


def add(rid: int, title: str, release_date: str | None, **fields) -> None:
    main.db.insert_release({"id": rid, "title": title, "edition": None, "studio": None, "year": None,
                            "release_date": release_date, "url": f"https://www.blu-ray.com/movies/x/{rid}/"},
                           seeding=True)
    with main.db.conn() as c:
        for key, value in fields.items():
            c.execute(f"UPDATE releases SET {key}=? WHERE id=?", (value, rid))


def test_icon_and_favicon_are_served(client):
    for path in ("/static/icon.png", "/favicon.ico"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    page = client.get("/")
    assert page.status_code == 200
    assert 'rel="icon"' in page.text


def test_released_title_is_not_shown_as_a_live_preorder(client):
    # "Today" is pinned to 2026-10-11 by the frozen_today fixture.
    add(9001, "Already Out 4K", "2026-10-01", is_steelbook=1, preorder=1, price="$29.99",
        availability="Available for pre-order")
    add(9002, "Still To Come 4K", "2026-12-01", is_steelbook=1, preorder=1, price="$31.27",
        availability="Available for pre-order")

    summary = client.get("/api/summary").json()
    assert summary["preorder"] == 1
    assert summary["next_title"] == "Still To Come 4K"
    assert summary["scanning"] is False

    preorders = client.get("/api/releases", params={"view": "preorder"}).json()
    assert [r["id"] for r in preorders] == [9002]

    page = client.get("/", params={"view": "all"}).text
    out, upcoming = page.split("Already Out 4K")[1].split("Still To Come 4K")
    assert "Released" in out and "Preorder live" not in out
    assert "Preorder live" in upcoming


def test_purchased_and_hidden_flags(client):
    add(9003, "Flag Me 4K", "2026-12-01", is_steelbook=1)
    assert client.post("/api/releases/9003/purchased", json={"value": True}).json() == {"id": 9003, "purchased": True}
    assert [r["id"] for r in client.get("/api/releases", params={"view": "purchased"}).json()] == [9003]
    assert 9003 not in [r["id"] for r in client.get("/api/releases").json()]
    client.post("/api/releases/9003/purchased", json={"value": False})
    assert 9003 in [r["id"] for r in client.get("/api/releases").json()]

    assert client.post("/api/releases/9003/deleted", json={"value": True}).status_code == 404
    assert client.post("/api/releases/123456789/hidden", json={"value": True}).status_code == 404


def test_digest_time_that_cannot_be_read_falls_back_to_nine():
    now = datetime(2026, 10, 11, 8, 0)
    assert main._seconds_until("09:30", now) == 90 * 60
    assert main._seconds_until("07:00", now) == 23 * 3600  # already past today
    for bad in ("", "9", "25:00", "09:75", "nine"):
        assert main._seconds_until(bad, now) == 3600, bad
