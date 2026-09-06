from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Awaitable, Callable, Literal
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from .config import settings
from .engine import (
    BrowserPool,
    DvaraError,
    extract,
    render,
    screenshot as engine_screenshot,
)
from .schemas import (
    ExtractRequest,
    ExtractResult,
    RenderRequest,
    ScreenshotRequest,
    WaitUntil,
    http_error,
)
from .sessions import SessionStore

log = logging.getLogger("dvara.main")

pool = BrowserPool()
sessions = SessionStore()


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO)
    await sessions.start()
    await pool.start()
    log.info("dvara pool up: %s browser x max %s concurrent", pool.size, settings.max_concurrent)
    try:
        yield
    finally:
        await pool.stop()
        await sessions.stop()


app = FastAPI(title="dvara", version="0.1.0", lifespan=lifespan)


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content=http_error(401, "unauthorized", "missing or invalid API key"))


def _check_auth(request: Request) -> JSONResponse | None:
    if not settings.api_key:
        return None
    key = request.headers.get("X-API-Key") or request.query_params.get("apikey")
    if key != settings.api_key:
        return _unauthorized()
    return None


def _proxy_opts(proxy: Any) -> dict[str, Any] | None:
    """Playwright proxies take credentials as separate fields, not in the server URL."""
    server = None
    username = password = None
    if isinstance(proxy, str):
        server = proxy
    elif proxy is not None:
        server = proxy.server
        username, password = proxy.username, proxy.password
    elif settings.default_proxy:
        server = settings.default_proxy
    if not server:
        return None
    u = urlparse(server)
    if u.username is not None:
        if username is None:
            username = unquote(u.username)
        if password is None and u.password is not None:
            password = unquote(u.password)
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        server = f"{u.scheme}://{host}{port}" if u.scheme else f"{host}{port}"
    opts: dict[str, Any] = {"server": server}
    if username:
        opts["username"] = username
    if password:
        opts["password"] = password
    return opts


async def _run(job: Callable[..., Awaitable[Any]], req: Any) -> Any:
    """Take a browser slot, restore session state, run the job, save state back."""
    timeout_s = req.timeout_s or settings.request_timeout_s
    async with pool.sem:
        browser = await pool.acquire()
        ctx_opts: dict[str, Any] = {}
        if po := _proxy_opts(req.proxy):
            ctx_opts["proxy"] = po
        if req.session_id:
            state = await sessions.get(req.session_id)
            if state:
                ctx_opts["storage_state"] = state

        ctx = await browser.new_context(**ctx_opts)
        try:
            page = await ctx.new_page()
            try:
                result = await asyncio.wait_for(
                    job(
                        page,
                        url=req.url,
                        wait_until=req.wait_until,
                        unwrap=req.unwrap,
                        wait_for=req.wait_for,
                        wait_timeout_ms=req.wait_timeout_ms,
                        delay_ms=req.delay_ms,
                        timeout_s=timeout_s,
                    ),
                    timeout=timeout_s + 5,
                )
            except asyncio.TimeoutError:
                raise DvaraError(504, "render_timeout", "render timed out")
            if req.session_id:
                try:
                    await sessions.set(req.session_id, await ctx.storage_state())
                except Exception:
                    log.exception("failed to save session %s", req.session_id)
            return result
        finally:
            try:
                await ctx.close()
            except Exception:
                log.exception("failed to close context")


def _error_response(exc: Exception) -> Response:
    if isinstance(exc, DvaraError):
        return JSONResponse(
            status_code=exc.status_code, content=http_error(exc.status_code, exc.code, str(exc))
        )
    log.exception("request failed")
    return JSONResponse(status_code=500, content=http_error(500, "internal", str(exc)))


# ---------------------------------------------------------------------------
# /v1/render
# ---------------------------------------------------------------------------


async def _render_common(req: Any) -> Response:
    res = await _run(render, req)
    headers = {
        "X-Dvara-Status": str(res["status"]),
        "X-Dvara-Final-Url": res["final_url"],
        "X-Dvara-Content-Url": res["content_url"],
    }
    if getattr(req, "response_format", "html") == "json":
        return JSONResponse(
            content={
                "url": res["url"],
                "status": res["status"],
                "final_url": res["final_url"],
                "content_url": res["content_url"],
                "title": res["title"],
                "content": res["content"],
            },
            headers=headers,
        )
    return Response(content=res["content"], media_type="text/html; charset=utf-8", headers=headers)


@app.post("/v1/render", tags=["render"], summary="Render a page to HTML")
async def post_render(req: RenderRequest, request: Request) -> Response:
    if auth := _check_auth(request):
        return auth
    try:
        return await _render_common(req)
    except Exception as e:
        return _error_response(e)


@app.get("/v1/render", tags=["render"], summary="Render a page to HTML (query params)")
async def get_render(
    request: Request,
    url: Annotated[str, Query(description="Target http(s) URL")],
    wait_until: Annotated[WaitUntil, Query()] = "load",
    wait_for: Annotated[str | None, Query(description="Wait for this CSS selector in the target frame")] = None,
    wait_timeout_ms: Annotated[int, Query(ge=500, le=120_000)] = 15_000,
    delay_ms: Annotated[int, Query(ge=0, le=60_000, description="Extra sleep (ms) after load")] = 0,
    session_id: Annotated[str | None, Query(description="Reuse a persisted browser session")] = None,
    proxy: Annotated[str | None, Query(description="Proxy URL, e.g. http://user:pass@host:port")] = None,
    unwrap: Annotated[bool, Query(description="Unwrap cloak/iframe wrapper pages")] = True,
    timeout_s: Annotated[int, Query(ge=5, le=300, description="Overall timeout (0 = server default)")] = 0,
    response_format: Annotated[Literal["html", "json"], Query()] = "html",
) -> Response:
    if auth := _check_auth(request):
        return auth
    try:
        req = RenderRequest(
            url=url,
            wait_until=wait_until,
            wait_for=wait_for,
            wait_timeout_ms=wait_timeout_ms,
            delay_ms=delay_ms,
            session_id=session_id,
            proxy=proxy,
            unwrap=unwrap,
            timeout_s=timeout_s,
            response_format=response_format,
        )
        return await _render_common(req)
    except Exception as e:
        return _error_response(e)


# ---------------------------------------------------------------------------
# /v1/extract
# ---------------------------------------------------------------------------


@app.post("/v1/extract", tags=["extract"], summary="Extract text from CSS selectors")
async def post_extract(req: ExtractRequest, request: Request) -> Response:
    if auth := _check_auth(request):
        return auth
    try:
        return await _extract_common(req)
    except Exception as e:
        return _error_response(e)


@app.get("/v1/extract", tags=["extract"], summary="Extract text from CSS selectors (query params)")
async def get_extract(
    request: Request,
    url: Annotated[str, Query(description="Target http(s) URL")],
    selectors: Annotated[str, Query(description='JSON object, e.g. {"h1": "h1", "prices": ".price"}')],
    max_results: Annotated[int, Query(ge=1, le=500)] = 100,
    wait_until: Annotated[WaitUntil, Query()] = "load",
    wait_for: Annotated[str | None, Query(description="Wait for this CSS selector in the target frame")] = None,
    wait_timeout_ms: Annotated[int, Query(ge=500, le=120_000)] = 15_000,
    delay_ms: Annotated[int, Query(ge=0, le=60_000, description="Extra sleep (ms) after load")] = 0,
    session_id: Annotated[str | None, Query(description="Reuse a persisted browser session")] = None,
    proxy: Annotated[str | None, Query(description="Proxy URL, e.g. http://user:pass@host:port")] = None,
    unwrap: Annotated[bool, Query(description="Unwrap cloak/iframe wrapper pages")] = True,
    timeout_s: Annotated[int, Query(ge=5, le=300, description="Overall timeout (0 = server default)")] = 0,
) -> Response:
    if auth := _check_auth(request):
        return auth
    try:
        req = ExtractRequest(
            url=url,
            selectors=json.loads(selectors),
            max_results=max_results,
            wait_until=wait_until,
            wait_for=wait_for,
            wait_timeout_ms=wait_timeout_ms,
            delay_ms=delay_ms,
            session_id=session_id,
            proxy=proxy,
            unwrap=unwrap,
            timeout_s=timeout_s,
        )
        return await _extract_common(req)
    except Exception as e:
        return _error_response(e)


async def _extract_common(req: Any) -> Response:
    res = await _run(
        lambda page, **kw: extract(page, selectors=req.selectors, max_results=req.max_results, **kw),
        req,
    )
    return JSONResponse(
        content=ExtractResult(
            url=res["url"],
            status=res["status"],
            final_url=res["final_url"],
            content_url=res["content_url"],
            data=res["data"],
        ).model_dump()
    )


# ---------------------------------------------------------------------------
# /v1/screenshot
# ---------------------------------------------------------------------------


@app.post("/v1/screenshot", tags=["screenshot"], summary="Capture a screenshot")
async def post_screenshot(req: ScreenshotRequest, request: Request) -> Response:
    if auth := _check_auth(request):
        return auth
    try:
        return await _screenshot_common(req)
    except Exception as e:
        return _error_response(e)


@app.get("/v1/screenshot", tags=["screenshot"], summary="Capture a screenshot (query params)")
async def get_screenshot(
    request: Request,
    url: Annotated[str, Query(description="Target http(s) URL")],
    format: Annotated[Literal["png", "jpeg"], Query()] = "png",
    quality: Annotated[int, Query(ge=1, le=100)] = 85,
    full_page: Annotated[bool, Query(description="Capture the full page height")] = False,
    wait_until: Annotated[WaitUntil, Query()] = "load",
    wait_for: Annotated[str | None, Query(description="Wait for this CSS selector in the target frame")] = None,
    wait_timeout_ms: Annotated[int, Query(ge=500, le=120_000)] = 15_000,
    delay_ms: Annotated[int, Query(ge=0, le=60_000, description="Extra sleep (ms) after load")] = 0,
    session_id: Annotated[str | None, Query(description="Reuse a persisted browser session")] = None,
    proxy: Annotated[str | None, Query(description="Proxy URL, e.g. http://user:pass@host:port")] = None,
    unwrap: Annotated[bool, Query(description="Unwrap cloak/iframe wrapper pages")] = True,
    timeout_s: Annotated[int, Query(ge=5, le=300, description="Overall timeout (0 = server default)")] = 0,
) -> Response:
    if auth := _check_auth(request):
        return auth
    try:
        req = ScreenshotRequest(
            url=url,
            format=format,
            quality=quality,
            full_page=full_page,
            wait_until=wait_until,
            wait_for=wait_for,
            wait_timeout_ms=wait_timeout_ms,
            delay_ms=delay_ms,
            session_id=session_id,
            proxy=proxy,
            unwrap=unwrap,
            timeout_s=timeout_s,
        )
        return await _screenshot_common(req)
    except Exception as e:
        return _error_response(e)


async def _screenshot_common(req: Any) -> Response:
    buf, media, status = await _run(
        lambda page, **kw: engine_screenshot(
            page, fmt=req.format, quality=req.quality, full_page=req.full_page, **kw
        ),
        req,
    )
    return Response(
        content=buf,
        media_type=media,
        headers={"X-Dvara-Status": str(status), "X-Dvara-Final-Url": req.url},
    )


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


@app.get("/health", tags=["system"])
async def health(request: Request) -> dict:
    if auth := _check_auth(request):
        return auth  # type: ignore[return-value]
    hp = await pool.health()
    return {"status": "ok", "pool": hp, "max_concurrent": settings.max_concurrent}


@app.post("/v1/session", tags=["sessions"], summary="Create a new browser session")
async def create_session(request: Request) -> Response:
    if auth := _check_auth(request):
        return auth
    sid = sessions.new_id()
    return JSONResponse(status_code=201, content={"session_id": sid})


@app.delete("/v1/session/{session_id}", tags=["sessions"], summary="Delete a browser session")
async def delete_session(session_id: str, request: Request) -> Response:
    if auth := _check_auth(request):
        return auth
    deleted = await sessions.delete(session_id)
    if not deleted:
        return JSONResponse(status_code=404, content=http_error(404, "not_found", "session not found"))
    return Response(status_code=204)
