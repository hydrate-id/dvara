# dvara

Anti-detect scraping engine as a REST API. Built on **Scrapling**'s stealth browser stack (patchright Chromium with anti-detection flags) so sites that block plain HTTP fetches (JS challenges, cookie redirects, wrapper/iframe cloaking) render like a real browser would.

Works like ScrapingAnt/ScrapingBee, but self-hosted: you keep the browser engine, proxy control, and the HTML out of any third-party dependency.

## Features

- Render a page to HTML (`/v1/render`)
- Extract text from CSS selectors (`/v1/extract`)
- Full-page or viewport screenshots (`/v1/screenshot`)
- Persistent browser sessions (cookies/localStorage) for logged-in or challenge-protected domains
- Auto **wrapper unwrapping**: many gambling/scam sites serve a tiny cloak page that embeds the real content in an iframe; dvara detects it, waits for the real frame to finish loading, and returns that content
- Optional proxy per request, or one default proxy for all traffic
- The fingerprint (UA, screen, flags) comes from Scrapling's stealth defaults and is the same for every request unless you change `context_options()` in `dvara/engine.py`
- Built for concurrency: a pool of browser processes, each serving multiple isolated incognito contexts

## Why plain fetch fails and this works

`requests`/`curl` against a protected site returns a stub page (500, JS spinner, or an empty `<title>@</title>` page). The real site is behind a chain like:

```
entry URL -> JS challenge (spinner) -> cloak page -> iframe /sttcs/ -> real content
```

The challenge sets cookies (e.g. `wtoken`, `wtime`), the cloak page loads a script that injects an iframe, and the iframe content grows into the real page over seconds. dvara:

1. Navigates with a real browser (Scrapling stealth Chromium, headless).
2. Sees the top document is a wrapper (small page with known markers).
3. Polls child frames until the largest one is stable across two reads.
4. Returns the real frame's HTML, or screenshots it by growing the iframe to full height.

## Architecture

```
Client ──> FastAPI routes (auth, validation)
                │
                ▼
            EngineService (async)
             ├── BrowserPool      N × Chromium processes (round-robin, auto-reload dead ones)
             ├── pipeline         goto -> challenge settle -> unwrap -> actions -> result
             └── SessionStore     SQLite (storage_state per session_id)
```

- Every request runs in its own incognito browser context. Nothing leaks between requests unless you reuse a `session_id`.
- One uvicorn worker is enough: work is I/O-bound. More workers multiply RAM, not throughput. Tune `DVARA_POOL_SIZE` instead.
- RAM is the real limit: roughly **300MB per browser process**.

## Requirements

- Python 3.12+ (managed with [uv](https://docs.astral.sh/uv/))
- macOS or Linux
- For Docker: Docker with BuildKit

## Quickstart (local dev)

```bash
uv sync
uv run patchright install chromium     # downloads the Chromium binary
DVARA_POOL_SIZE=2 uv run uvicorn dvara.main:app --reload
```

Open http://localhost:8080/docs for the interactive API browser.

## Configuration

Environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `DVARA_API_KEY` | empty | Static API key. Empty = no auth (dev/internal). |
| `DVARA_HOST` | `0.0.0.0` | Bind address. |
| `DVARA_PORT` | `8080` | Bind port. |
| `DVARA_POOL_SIZE` | `2` | Concurrent Chromium processes (~300MB each). VPS RAM: 4GB→4, 8GB→8, 16GB→16. |
| `DVARA_MAX_CONCURRENT` | `8` | Max tasks running at once (each = one context). |
| `DVARA_TIMEOUT_S` | `70` | Default per-request timeout (seconds). |
| `DVARA_DB` | `dvara.db` | SQLite session database path. |
| `DVARA_HEADLESS` | `1` | `0` shows a browser window (debugging only). |
| `DVARA_DEVICE_SCALE_FACTOR` | `1` | Pixel density. `2` gives retina screenshots (4x pixels, larger and slower). |
| `DVARA_BROWSER_EXTRA` | empty | Extra Chromium flags, space separated. |
| `DVARA_DEFAULT_PROXY` | empty | Proxy used when a request sends none. |

## API

Authentication: send `X-API-Key: <key>` header or `?apikey=<key>` query. Without `DVARA_API_KEY` set, everything is open.

All endpoints accept **GET** (query params) and **POST** (JSON body). `url` is always required.

Common options (both methods):

| Field | Default | Description |
|---|---|---|
| `wait_until` | `load` | `load`, `domcontentloaded`, or `networkidle`. |
| `wait_for` | none | CSS selector; waits until it exists in the target frame before returning. |
| `wait_timeout_ms` | `15000` | How long to wait for `wait_for`. |
| `delay_ms` | `0` | Extra sleep after page load (lets lazy JS finish). |
| `unwrap` | `true` | Auto-detect cloak wrapper and return the real iframe content. `false` returns the raw top document. |
| `proxy` | none | Proxy for this request. String URL or `{"server","username","password"}`. Credentials may also live in the URL. |
| `session_id` | none | Persist/restore cookies & localStorage for this id. |
| `timeout_s` | `0` | Overall timeout (5–300). `0` = `DVARA_TIMEOUT_S`. |

### Render HTML

`GET|POST /v1/render`

POST example:

```bash
curl -X POST http://localhost:8080/v1/render \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "response_format": "json"}'
```

- Default response: the HTML body (`text/html`).
- `response_format: "json"` returns:
  ```json
  {"url": "...", "status": 200, "final_url": "...", "content_url": "...", "title": "...", "content": "<html>..."}
  ```
- `X-Dvara-Status` / `X-Dvara-Final-Url` / `X-Dvara-Content-Url` headers are set on both formats. `content_url` points at the frame the content came from (differs from `final_url` when a wrapper was unwrapped).

### Extract text

`GET|POST /v1/extract`

```bash
curl -X POST http://localhost:8080/v1/extract \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "selectors": {"headline": "h1", "prices": ".price"}, "max_results": 100}'
```

Response:

```json
{"url": "...", "status": 200, "final_url": "...", "content_url": "...", "data": {"headline": ["..."], "prices": ["...", "..."]}}
```

GET equivalent sends `selectors` as a URL-encoded JSON string: `?selectors=%7B%22h1%22%3A%22h1%22%7D`.

### Screenshot

`GET|POST /v1/screenshot`

```bash
curl -X POST http://localhost:8080/v1/screenshot \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "format": "jpeg", "full_page": true, "quality": 85}' \
  --output shot.jpeg
```

- `format`: `png` (default) or `jpeg`.
- `full_page`: when the target was unwrapped from a cloak iframe, dvara grows the iframe to full height first so the whole real page is captured.

### Sessions

```bash
# create
curl -X POST http://localhost:8080/v1/session          # -> {"session_id": "..."}
# use: pass session_id in any render/extract/screenshot request
# delete
curl -X DELETE http://localhost:8080/v1/session/<id>   # 204, or 404
```

Cookies and localStorage are snapshotted to SQLite after every request that used a `session_id`, and restored before the next one. Typical flow for a challenge-protected domain: first request clears the challenge (optionally hitting it once with no content expected), then follow-up requests reuse the session and skip re-challenging.

### Health

```bash
curl http://localhost:8080/health
# {"status": "ok", "pool": {"size": 2, "alive": 2}, "max_concurrent": 8}
```

## Errors

Non-2xx responses return JSON: `{"error": {"code": "...", "message": "..."}}`.

| Status | Code | When |
|---|---|---|
| `401` | `unauthorized` | Wrong/missing API key. |
| `422` | (validation) | Bad request body/params. |
| `504` | `render_timeout` / `wait_for_timeout` | Overall timeout, or a `wait_for` selector never appeared. |
| `500` | `internal` | Unexpected failure (browser crash, pool exhausted, etc.). |

A target site returning 4xx/5xx is **not** an error: the page is rendered and returned (with its real status in `X-Dvara-Status` / the `status` field). Timeouts and internal failures are the only error paths.

## Proxy

Per request:

```json
{"url": "...", "proxy": "http://user:pass@host:port"}
```

or

```json
{"url": "...", "proxy": {"server": "http://host:port", "username": "user", "password": "pass"}}
```

When using a proxy for geo-targeted scraping, note that the browser locale and timezone follow the host and may not match your exit country.

## Docker

```bash
docker compose up -d --build
```

- Installs Chromium and its system libraries into the image with `patchright install --with-deps chromium`.
- Playwright publishes Chromium builds for x86_64 and arm64, so no arch setting is needed (only x86_64 has been tested here).
- `compose.yaml` forwards the `DVARA_*` variables above (API key, pool, timeout, proxy, device scale factor, extra flags). Set them in `.env` or your shell.
- Sessions persist in the `dvara-data` volume (`/data`).
- `mem_limit` defaults to 4g; raise it with your pool size.

## Tests

Self-contained checks (local HTTP target, no internet):

```bash
uv run python tests/test_engine.py   # engine: render, unwrap, extract, screenshot, session store
uv run python tests/test_api.py      # HTTP API: auth, render/extract/screenshot, session cookies, proxy parsing
```

## Tuning

- Watch `/health` → `pool.alive`. If a browser keeps dying, your VPS is out of RAM; lower `DVARA_POOL_SIZE`.
- Challenge-protected sites need several seconds each (challenge settles in ~5–15s). Concurrency matters more than speed per request: raise `DVARA_MAX_CONCURRENT` only as far as RAM allows.
- Wrapper polling only runs while the top page *looks like* a challenge page; normal sites return on the first scan, so `unwrap: true` adds no delay for them.

## Limitations

- Scrapling's stealth Chromium is not a guarantee. Advanced WAFs (e.g. Cloudflare Interstitial) can still block it.
- Some cloaked gambling domains do not show content directly: the entry domain resolves to a list of active mirror domains (often obfuscated in base64) served from a CDN. dvara returns what the browser renders at the entry; chasing mirror hops to the final brand page is up to the consuming service.
- The fingerprint is not randomized: every request uses the same Scrapling defaults (UA, viewport). Launch flags are fixed per browser process (`DVARA_BROWSER_EXTRA`); UA and viewport are set per context in `context_options()`.
