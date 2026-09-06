# dvara

Anti-detect scraping engine as a REST API. Built on **Camoufox** (a stealth-patched Firefox) so sites that block plain HTTP fetches — JS challenges, cookie redirects, wrapper/iframe cloaking — render like a real browser would.

Works like ScrapingAnt/ScrapingBee, but self-hosted: you keep the browser engine, proxy control, and the HTML out of any third-party dependency.

## Features

- Render a page to HTML (`/v1/render`)
- Extract text from CSS selectors (`/v1/extract`)
- Full-page or viewport screenshots (`/v1/screenshot`)
- Persistent browser sessions (cookies/localStorage) for logged-in or challenge-protected domains
- Auto **wrapper unwrapping**: many gambling/scam sites serve a tiny cloak page that embeds the real content in an iframe; dvara detects it, waits for the real frame to finish loading, and returns that content
- Optional proxy per request, or one default proxy for all traffic
- Camoufox fingerprints (OS, UA, screen, etc.) are randomized per browser process and cannot be changed at runtime
- Built for concurrency: a pool of browser processes, each serving multiple isolated incognito contexts

## Why plain fetch fails and this works

`requests`/`curl` against a protected site returns a stub page (500, JS spinner, or an empty `<title>@</title>` page). The real site is behind a chain like:

```
entry URL -> JS challenge (spinner) -> cloak page -> iframe /sttcs/ -> real content
```

The challenge sets cookies (e.g. `wtoken`, `wtime`), the cloak page loads a script that injects an iframe, and the iframe content grows into the real page over seconds. dvara:

1. Navigates with a real browser (Camoufox, headless).
2. Sees the top document is a wrapper (small page with known markers).
3. Polls child frames until the largest one is stable across two reads.
4. Returns the real frame's HTML, or screenshots it by growing the iframe to full height.

## Architecture

```
Client ──> FastAPI routes (auth, validation)
                │
                ▼
            EngineService (async)
             ├── BrowserPool      N × Camoufox processes (round-robin, auto-reload dead ones)
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
uv run python -m camoufox fetch        # downloads the Camoufox browser binary
DVARA_POOL_SIZE=2 uv run uvicorn dvara.main:app --reload
```

Open http://localhost:8080/docs for the interactive API browser.

Note: `camoufox fetch` lists releases through the GitHub API and can hit the rate limit. Retry after an hour, or install the browser binary manually (the Dockerfile does this and is a reference for the layout).

## Configuration

Environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `DVARA_API_KEY` | empty | Static API key. Empty = no auth (dev/internal). |
| `DVARA_HOST` | `0.0.0.0` | Bind address. |
| `DVARA_PORT` | `8080` | Bind port. |
| `DVARA_POOL_SIZE` | `2` | Concurrent Firefox processes (~300MB each). VPS RAM: 4GB→4, 8GB→8, 16GB→16. |
| `DVARA_MAX_CONCURRENT` | `8` | Max tasks running at once (each = one context). |
| `DVARA_TIMEOUT_S` | `70` | Default per-request timeout (seconds). |
| `DVARA_DB` | `dvara.db` | SQLite session database path. |
| `DVARA_HEADLESS` | `1` | `0` shows a browser window (debugging only). |
| `DVARA_DEFAULT_PROXY` | empty | Proxy used when a request sends none. |

## API

Authentication: send `X-API-Key: <key>` header or `?apikey=<key>` query. Without `DVARA_API_KEY` set, everything is open.

All endpoints accept **GET** (query params) and **POST** (JSON body). `url` is always required.

Common options (both methods):

| Field | Default | Description |
|---|---|---|
| `wait_until` | `load` | `load`, `domcontentloaded`, or `networkidle`. |
| `wait_for` | — | CSS selector; waits until it exists in the target frame before returning. |
| `wait_timeout_ms` | `15000` | How long to wait for `wait_for`. |
| `delay_ms` | `0` | Extra sleep after page load (lets lazy JS finish). |
| `unwrap` | `true` | Auto-detect cloak wrapper and return the real iframe content. `false` returns the raw top document. |
| `proxy` | — | Proxy for this request. String URL or `{"server","username","password"}`. Credentials may also live in the URL. |
| `session_id` | — | Persist/restore cookies & localStorage for this id. |
| `timeout_s` | `0` | Overall timeout (5–300). `0` = `DVARA_TIMEOUT_S`. |

### Render HTML

`GET|POST /v1/render`

POST example:

```bash
curl -X POST http://localhost:8080/v1/render \
  -H "Content-Type: application/json" \
  -d '{"url": "https://1530ofarrellst1.com", "response_format": "json"}'
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

When using a proxy for geo-targeted scraping, set Camoufox's `os`/`locale` expectations yourself if needed — by default fingerprints are drawn from real-world distribution and may not match your exit country.

## Docker

```bash
docker compose up -d --build
```

- Bakes the Camoufox browser binary into the image (pinned `CAMOUFOX_VERSION` in the `Dockerfile`/`compose.yaml`, so builds do not hit GitHub API rate limits).
- Change `CAMOUFOX_ARCH` to `arm64` for ARM nodes.
- Sessions persist in the `dvara-data` volume (`/data`).
- `mem_limit` defaults to 4g; raise it with your pool size.

## Tests

Self-contained engine checks (local HTTP target, no internet):

```bash
uv run python tests/test_engine.py
```

Covers: normal-site rendering, wrapper unwrap, extract on the real frame, screenshot, and the SQLite session store.

## Tuning

- Watch `/health` → `pool.alive`. If a browser keeps dying, your VPS is out of RAM; lower `DVARA_POOL_SIZE`.
- Challenge-protected sites need several seconds each (challenge settles in ~5–15s). Concurrency matters more than speed per request: raise `DVARA_MAX_CONCURRENT` only as far as RAM allows.
- Wrapper polling only runs while the top page *looks like* a challenge page; normal sites return on the first scan, so `unwrap: true` adds no delay for them.

## Limitations

- Camoufox is an anti-detect browser, not a guarantee. Advanced WAFs (e.g. Cloudflare Interstitial, which probes the JS engine) can still block it. Upstream Camoufox is under active development.
- Some cloaked gambling domains do not show content directly: the entry domain resolves to a list of active mirror domains (often obfuscated in base64) served from a CDN. dvara returns what the browser renders at the entry; chasing mirror hops to the final brand page is up to the consuming service.
- Fingerprints are per browser process and fixed at launch. Per-request OS/UA spoofing is not possible with a pooled design; recycle the whole process to change identity.
