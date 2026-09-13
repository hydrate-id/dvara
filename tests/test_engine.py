"""Self-check of the dvara engine against a local HTTP target (no internet).

Covered:
  1. normal site -> render the top document directly (not a wrapper)
  2. gambling-style wrapper (/wrap + iframe /inner) -> unwrap to real content
  3. extract runs its selectors against the real frame
  4. screenshot produces a non-empty PNG
  5. SQLite session store persists / restores / deletes

Note: Gambling-themed HTML below is fixture data for testing only.

Run with: uv run python tests/test_engine.py
"""

import asyncio
import http.server
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dvara.engine import BrowserPool, extract, render, screenshot
from dvara.sessions import SessionStore

INNER = b"""<!DOCTYPE html><html><head><title>Situs Judol 88</title></head>
<body><h1>Main Judol</h1><p>JACKPOT SLOT BOLA deposit minimal 10rb</p>
<a href="/">login</a><div class="games"><span>slot zeus</span><span>kakek</span>
<span>starlight princess</span><span>mahjong ways</span><span>gates of olympus</span>
</div>""" + (b"<p>" + b"isi artikel panjang menyerupai laman situs asli. " * 300 + b"</p>") + b"</body></html>"

WRAPPER = b"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>@</title></head><body><div style="opacity:0">
<script src="/sttcs/stjs.js"></script><iframe src="/inner"></iframe>
</div></body></html>"""

PLAIN = b"""<!DOCTYPE html><html><head><title>Blog Pribadi</title></head>
<body><h1>Halo dunia</h1><p>Isi artikel biasa</p></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/wrap") or self.path.startswith("/sttcs"):
            body = WRAPPER
        elif self.path.startswith("/inner"):
            body = INNER
        elif self.path.startswith("/plain"):
            body = PLAIN
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass


def _start_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def main():
    srv = _start_server()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    pool = BrowserPool(size=1)
    store = SessionStore()
    try:
        await store.start()
        await pool.start()
        browser = await pool.acquire()

        # 1. normal site: top document is returned directly, not a wrapper
        ctx = await browser.new_context()
        page = await ctx.new_page()
        r = await render(page, f"{base}/plain", "load", True, None, 10_000, 0, 30)
        assert r["content_url"] == f"{base}/plain", r["content_url"]
        assert "Halo dunia" in r["content"], "normal site content missing"
        print("ok 1 normal site -> top document")

        # 2. gambling wrapper: unwrap to the real iframe
        r = await render(page, f"{base}/wrap", "load", True, None, 10_000, 0, 30)
        assert "sttcs" in r["content_url"] or "inner" in r["content_url"], r["content_url"]
        assert "JACKPOT SLOT" in r["content"], "real frame content not unwrapped"
        print("ok 2 wrapper -> unwrapped real content (len %s)" % len(r["content"]))

        # 3. extract runs selectors against the real frame
        r = await extract(page, f"{base}/wrap", "load", True, None, 10_000, 0, 30,
                          {"h1": "h1", "games": ".games span"}, 20)
        assert r["data"]["h1"] == ["Main Judol"], r["data"]
        assert any("zeus" in g for g in r["data"]["games"]), r["data"]
        print("ok 3 extract selectors on the real frame")

        # 4. screenshot produces a PNG
        buf, media, _ = await screenshot(page, f"{base}/wrap", "load", True, None, 10_000, 0, 30,
                                         "png", 85, False)
        assert media == "image/png" and buf[:8] == b"\x89PNG\r\n\x1a\n", "bad PNG header"
        print("ok 4 screenshot png (%s bytes)" % len(buf))
        await ctx.close()

        # 5. session store
        sid = store.new_id()
        assert await store.get(sid) is None
        await store.set(sid, {"cookies": [{"name": "wtoken"}]})
        st = await store.get(sid)
        assert st and st["cookies"][0]["name"] == "wtoken"
        assert await store.delete(sid) and await store.get(sid) is None
        print("ok 5 sqlite session store")

        await pool.stop()
        await store.stop()
        print("\nALL PASS")
    finally:
        await pool.stop()
        await store.stop()
        srv.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
