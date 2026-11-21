"""A stand-in for a Shopify shop on localhost, for the browser reader's tests.

It serves what the reader touches: a collection page with a stylesheet, an image and a
script, and the product list of each collection. Every request is recorded, with its
headers, so a test can say what the browser asked for and how. Only the standard
library is used, so the same stand-in runs inside the Docker image
(tests/browser_check.py).
"""

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PAGE = """<!doctype html>
<html><head><title>{name}</title><link rel="stylesheet" href="/theme.css"></head>
<body><h1>{name}</h1><img src="/logo.png" alt="">
<script>
  // What a shop's scripts could look at to tell a program from a person.
  fetch("/seen?webdriver=" + navigator.webdriver, {{method: "POST"}});
</script></body></html>"""
CHECK = "<!doctype html><title>Verify</title><h1>Your connection needs to be verified before you can proceed</h1>"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


@dataclass
class Seen:
    method: str
    path: str
    query: dict
    headers: dict  # names in lower case


class Shopfront:
    def __init__(self, lists: dict[str, list] | None = None):
        # Product lists by address, e.g. "/collections/steelbooks/products.json".
        self.lists = lists or {}
        # Addresses that answer something else: path -> (status, headers, body).
        self.answers: dict[str, tuple[int, dict, str]] = {}
        self.seen: list[Seen] = []
        self.visitors = 0
        self._server: ThreadingHTTPServer | None = None

    # --- what a test asks about ---------------------------------------------
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def paths(self) -> list[str]:
        return [s.path for s in self.seen]

    def last(self, path: str) -> Seen:
        return [s for s in self.seen if s.path == path][-1]

    # --- running ----------------------------------------------------------------
    def __enter__(self) -> "Shopfront":
        front = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                front._answer(self)

            do_POST = do_GET

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _answer(self, request: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(request.path)
        path = parts.path
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.seen.append(Seen(request.command, path, parse_qs(parts.query), headers))
        extra = {}
        if path in self.answers:
            status, extra, body = self.answers[path]
            kind = "text/html; charset=utf-8"
        elif path in self.lists:
            status, kind, body = 200, "application/json; charset=utf-8", self._list(path, parse_qs(parts.query))
        elif path.startswith("/collections/"):
            status, kind = 200, "text/html; charset=utf-8"
            body = PAGE.format(name=path.rsplit("/", 1)[-1])
            if "visitor=" not in headers.get("cookie", ""):
                # The kind of cookie a shop uses to recognise a returning visitor.
                self.visitors += 1
                extra = {"Set-Cookie": f"visitor={self.visitors}; Max-Age=31536000; Path=/"}
        elif path == "/challenge":
            status, kind, body = 200, "text/html; charset=utf-8", CHECK
        elif path == "/theme.css":
            status, kind, body = 200, "text/css", "h1 { font-family: sans-serif }"
        elif path == "/logo.png":
            status, kind, body = 200, "image/png", PNG
        elif path == "/seen":
            status, kind, body = 204, "text/plain", ""
        else:
            status, kind, body = 404, "text/plain", "Not found"
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        request.send_response(status)
        request.send_header("Content-Type", kind)
        request.send_header("Content-Length", str(len(data)))
        for name, value in extra.items():
            request.send_header(name, value)
        request.end_headers()
        if status != 204:
            request.wfile.write(data)

    def _list(self, path: str, query: dict) -> str:
        limit = int(query.get("limit", ["30"])[0])
        page = int(query.get("page", ["1"])[0])
        return json.dumps({"products": self.lists[path][(page - 1) * limit: page * limit]})
