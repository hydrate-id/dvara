"""HTTP-level self-check of the dvara API against a local target (no internet).

Covered: /health, API-key auth, GET/POST render, extract, screenshot,
session_id cookie round-trip, GET without timeout_s, proxy credential parsing.

Run with: uv run python tests/test_api.py
"""

import asyncio
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DVARA_POOL_SIZE"] = "1"
os.environ["DVARA_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DVARA_API_KEY"] = "secret"

import uvicorn  # noqa: E402

from dvara.main import _proxy_opts, app  # noqa: E402
from dvara.schemas import Proxy  # noqa: E402

PLAIN = b"<html><head><title>Plain</title></head><body><h1>Halo dunia</h1></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        headers = {}
        if self.path.startswith("/setcookie"):
            body = PLAIN
            headers["Set-Cookie"] = "wtoken=abc123; Path=/"
        elif self.path.startswith("/whoami"):
            body = ("<html><body><p id='c'>cookie=%s</p></body></html>" % self.headers.get("Cookie", "")).encode()
        elif self.path.startswith("/plain"):
            body = PLAIN
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(method, url, key="secret", body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None)
    if key:
        req.add_header("X-API-Key", key)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


async def acall(*a, **kw):
    return await asyncio.to_thread(call, *a, **kw)


async def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    target = f"http://127.0.0.1:{srv.server_address[1]}"

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    api = f"http://127.0.0.1:{port}"
    try:
        while not server.started:
            await asyncio.sleep(0.1)

        # 1. health + auth
        st, body = await acall("GET", f"{api}/health")
        assert st == 200 and json.loads(body)["pool"]["alive"] == 1, (st, body)
        st, _ = await acall("GET", f"{api}/health", key=None)
        assert st == 401, st
        print("ok 1 health + auth")

        # 2. GET render without timeout_s (used to 422), with response_format=json
        st, body = await acall("GET", f"{api}/v1/render?url={target}/plain&response_format=json")
        assert st == 200, (st, body)
        data = json.loads(body)
        assert data["title"] == "Plain" and "Halo dunia" in data["content"], data
        print("ok 2 GET render (no timeout_s)")

        # 3. POST render + invalid timeout rejected
        st, body = await acall("POST", f"{api}/v1/render", body={"url": f"{target}/plain"})
        assert st == 200 and b"Halo dunia" in body, (st, body)
        st, _ = await acall("POST", f"{api}/v1/render", body={"url": f"{target}/plain", "timeout_s": 2})
        assert st == 422, st
        print("ok 3 POST render + timeout validation")

        # 4. extract
        st, body = await acall("POST", f"{api}/v1/extract",
                               body={"url": f"{target}/plain", "selectors": {"h1": "h1"}})
        assert st == 200 and json.loads(body)["data"]["h1"] == ["Halo dunia"], (st, body)
        print("ok 4 extract")

        # 5. screenshot
        st, body = await acall("GET", f"{api}/v1/screenshot?url={target}/plain")
        assert st == 200 and body[:8] == b"\x89PNG\r\n\x1a\n", st
        print("ok 5 screenshot")

        # 6. session cookie round-trip
        st, body = await acall("POST", f"{api}/v1/session")
        sid = json.loads(body)["session_id"]
        await acall("POST", f"{api}/v1/render", body={"url": f"{target}/setcookie", "session_id": sid})
        st, body = await acall("POST", f"{api}/v1/render", body={"url": f"{target}/whoami", "session_id": sid})
        assert b"wtoken=abc123" in body, body
        st, body = await acall("POST", f"{api}/v1/render", body={"url": f"{target}/whoami"})
        assert b"wtoken" not in body, "cookie leaked into a request without session_id"
        assert (await acall("DELETE", f"{api}/v1/session/{sid}"))[0] == 204
        print("ok 6 session cookie persisted and isolated")

        # 7. proxy credential parsing
        o = _proxy_opts("http://user:p%40ss@proxy.example:8080")
        assert o == {"server": "http://proxy.example:8080", "username": "user", "password": "p@ss"}, o
        o = _proxy_opts(Proxy(server="socks5://h:1", username="u", password="p"))
        assert o == {"server": "socks5://h:1", "username": "u", "password": "p"}, o
        assert _proxy_opts(None) is None
        print("ok 7 proxy parsing")

        print("\nALL PASS")
    finally:
        server.should_exit = True
        await task
        srv.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
