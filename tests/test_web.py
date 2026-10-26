import importlib


def test_icon_and_favicon_are_served(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    main = importlib.import_module("app.main")
    client = TestClient(main.app)  # no lifespan: the scheduler doesn't start

    for path in ("/static/icon.png", "/favicon.ico"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    page = client.get("/")
    assert page.status_code == 200
    assert 'rel="icon"' in page.text
