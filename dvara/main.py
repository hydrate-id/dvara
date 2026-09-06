from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, Request
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


def _to_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes", "on")


async def _read_body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _from_query(request: Request, fields: dict[str, Any]) -> dict:
    out: dict[str, Any] = {}
    q = request.query_params
    for key, typ in fields.items():
        if key not in q:
            continue
        val: Any = q[key]
        if typ is bool or typ == bool | None:
            val = _to_bool(val)
        elif typ in (int, int | None) and val:
            try:
                val = int(val)
            except ValueError:
                continue
        out[key] = val
    return out


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


async def _render_endpoint(request: Request) -> Response:
    if auth := _check_auth(request):
        return auth
    try:
        if request.method == "GET":
            from .schemas import RENDER_QUERY

            req = RenderRequest.model_validate(_from_query(request, RENDER_QUERY))
        else:
            body = await _read_body(request)
            req = RenderRequest.model_validate(body)
        res = await _run(render, req)
        headers = {
            "X-Dvara-Status": str(res["status"]),
            "X-Dvara-Final-Url": res["final_url"],
            "X-Dvara-Content-Url": res["content_url"],
        }
        if req.response_format == "json":
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
    except DvaraError as e:
        return JSONResponse(status_code=e.status_code, content=http_error(e.status_code, e.code, str(e)))
    except Exception as e:  # noqa: BLE001
        log.exception("render failed")
        return JSONResponse(status_code=500, content=http_error(500, "internal", str(e)))


async def _extract_endpoint(request: Request) -> Response:
    if auth := _check_auth(request):
        return auth
    try:
        if request.method == "GET":
            q = _from_query(request, {})
            q["selectors"] = json.loads(request.query_params.get("selectors", "{}"))
            if request.query_params.get("max_results"):
                q["max_results"] = int(request.query_params["max_results"])
            req = ExtractRequest.model_validate(q)
        else:
            body = await _read_body(request)
            req = ExtractRequest.model_validate(body)
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
    except DvaraError as e:
        return JSONResponse(status_code=e.status_code, content=http_error(e.status_code, e.code, str(e)))
    except Exception as e:  # noqa: BLE001
        log.exception("extract failed")
        return JSONResponse(status_code=500, content=http_error(500, "internal", str(e)))


async def _screenshot_endpoint(request: Request) -> Response:
    if auth := _check_auth(request):
        return auth
    try:
        if request.method == "GET":
            from .schemas import SCREENSHOT_QUERY

            req = ScreenshotRequest.model_validate(_from_query(request, SCREENSHOT_QUERY))
        else:
            body = await _read_body(request)
            req = ScreenshotRequest.model_validate(body)
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
    except DvaraError as e:
        return JSONResponse(status_code=e.status_code, content=http_error(e.status_code, e.code, str(e)))
    except Exception as e:  # noqa: BLE001
        log.exception("screenshot failed")
        return JSONResponse(status_code=500, content=http_error(500, "internal", str(e)))


@app.get("/health")
async def health(request: Request) -> dict:
    if auth := _check_auth(request):
        return auth  # type: ignore[return-value]
    hp = await pool.health()
    return {"status": "ok", "pool": hp, "max_concurrent": settings.max_concurrent}


@app.api_route("/v1/render", methods=["GET", "POST"])
async def v1_render(request: Request) -> Response:
    return await _render_endpoint(request)


@app.api_route("/v1/extract", methods=["GET", "POST"])
async def v1_extract(request: Request) -> Response:
    return await _extract_endpoint(request)


@app.api_route("/v1/screenshot", methods=["GET", "POST"])
async def v1_screenshot(request: Request) -> Response:
    return await _screenshot_endpoint(request)


@app.post("/v1/session")
async def create_session(request: Request) -> Response:
    if auth := _check_auth(request):
        return auth
    sid = sessions.new_id()
    return JSONResponse(status_code=201, content={"session_id": sid})


@app.delete("/v1/session/{session_id}")
async def delete_session(session_id: str, request: Request) -> Response:
    if auth := _check_auth(request):
        return auth
    deleted = await sessions.delete(session_id)
    if not deleted:
        return JSONResponse(status_code=404, content=http_error(404, "not_found", "session not found"))
    return Response(status_code=204)
