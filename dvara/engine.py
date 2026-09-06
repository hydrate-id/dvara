from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Browser, Frame, Page, TimeoutError as PWTimeoutError

from .config import settings

log = logging.getLogger("dvara.engine")

WRAPPER_MARKERS = ("sttcs", "安全跳转", "<title>@</title>")


def _looks_like_wrapper(html: str) -> bool:
    if len(html) > 25_000:
        return False
    low = html[:4_000].lower()
    return any(m in html or m.lower() in low for m in WRAPPER_MARKERS)


@dataclass
class _Slot:
    index: int
    cam: AsyncCamoufox | None = None
    browser: Browser | None = None

    async def launch(self) -> None:
        if self.cam is not None:
            await self.cam.__aexit__(None, None, None)
            self.cam = None
        self.browser = None
        cam = AsyncCamoufox(headless=settings.headless)
        try:
            browser = await cam.__aenter__()
        except BaseException:
            await cam.__aexit__(None, None, None)
            raise
        self.cam, self.browser = cam, browser

    async def stop(self) -> None:
        if self.cam is not None:
            try:
                await self.cam.__aexit__(None, None, None)
            finally:
                self.cam = None
                self.browser = None


class BrowserPool:
    def __init__(self, size: int | None = None) -> None:
        self.size = size or settings.pool_size
        self._slots: list[_Slot] = []
        self._rr = 0
        self.sem = asyncio.Semaphore(settings.max_concurrent)

    async def start(self) -> None:
        self._slots = [_Slot(i) for i in range(self.size)]
        await asyncio.gather(*(s.launch() for s in self._slots))

    async def stop(self) -> None:
        await asyncio.gather(*(s.stop() for s in self._slots), return_exceptions=True)

    async def _healthy(self, s: _Slot) -> bool:
        try:
            return s.browser is not None and s.browser.is_connected()
        except Exception:
            return False

    async def acquire(self) -> Browser:
        """Pick a browser round-robin; relaunch dead slots."""
        for _ in range(self.size):
            s = self._slots[self._rr % self.size]
            self._rr += 1
            if not await self._healthy(s):
                try:
                    await s.launch()
                except Exception:
                    log.exception("relaunch browser slot %s failed", s.index)
                    continue
            return s.browser  # type: ignore[return-value]
        raise RuntimeError("all browsers in pool failed to launch")

    async def health(self) -> dict[str, Any]:
        states = []
        for s in self._slots:
            states.append(await self._healthy(s))
        return {"size": len(states), "alive": sum(states)}


def _settle_budget(timeout_s: int) -> float:
    """Time budget for wrapper polling. Normal sites exit on the first scan,
    so this only applies while the top document looks like a challenge page."""
    return min(20.0, max(2.0, timeout_s - 10))


async def _resolve_target(page: Page, unwrap: bool, settle_s: float) -> tuple[Frame, str]:
    """Pick the frame that holds the real content.

    Gambling sites serve a chain of challenge pages (spinner -> '@' wrapper ->
    /sttcs/ iframe -> real content), and the iframe content grows over time
    (13KB -> 2.6MB). When the top document looks like a wrapper, find the
    largest child frame and wait until its size is stable over two reads.
    Normal sites exit on the first scan.
    """
    if not unwrap:
        top = page.main_frame
        return top, await top.content()

    deadline = asyncio.get_event_loop().time() + settle_s
    best: Frame | None = None
    best_len = 0
    best_html = ""
    seen_len = -1  # best_len at last stable read

    while True:
        top = page.main_frame
        top_html = await top.content()
        if not _looks_like_wrapper(top_html):
            return top, top_html
        top_len = len(top_html)

        cand: Frame | None = None
        cand_len = top_len
        cand_html = top_html
        for fr in page.frames:
            if fr == top:
                continue
            try:
                html = await fr.content()
            except Exception:
                continue
            if len(html) > cand_len:
                cand, cand_len, cand_html = fr, len(html), html

        real = cand is not None and cand_len > top_len * 3
        if real and cand is best and cand_len == seen_len:
            return best, best_html  # type: ignore[return-value]

        if cand is not None and (best is None or cand_len > best_len):
            best, best_len, best_html = cand, cand_len, cand_html
            seen_len = cand_len if best_len == seen_len else -1
        elif best is not None and cand is best:
            seen_len = cand_len
        else:
            seen_len = -1

        if asyncio.get_event_loop().time() >= deadline:
            if real:
                return best, best_html  # type: ignore[return-value]
            return top, top_html
        await asyncio.sleep(0.6)


class DvaraError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


async def _await_frame_ready(frame: Frame, wait_for: str | None, timeout_ms: int) -> None:
    if not wait_for:
        return
    try:
        await frame.wait_for_selector(wait_for, timeout=timeout_ms, state="attached")
    except PWTimeoutError:
        raise DvaraError(504, "wait_for_timeout", f"selector did not appear within {timeout_ms}ms")


async def _eval_texts(frame: Frame, selector: str, limit: int) -> list[str]:
    return await frame.evaluate(
        """(arg) => {
            const out = [];
            for (const el of document.querySelectorAll(arg.sel)) {
                if (out.length >= arg.limit) break;
                out.push((el.innerText || el.textContent || '').trim());
            }
            return out;
        }""",
        {"sel": selector, "limit": limit},
    )


async def render(page: Page, url: str, wait_until: str, unwrap: bool, wait_for: str | None,
                 wait_timeout_ms: int, delay_ms: int, timeout_s: int) -> dict[str, Any]:
    resp = await page.goto(url, wait_until=wait_until, timeout=timeout_s * 1000)
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)
    target, _ = await _resolve_target(page, unwrap, _settle_budget(timeout_s))
    await _await_frame_ready(target, wait_for, wait_timeout_ms)
    html = await target.content()
    try:
        title = await target.title()
    except Exception:
        title = ""
    return {
        "url": url,
        "status": resp.status if resp else 0,
        "final_url": page.url,
        "content_url": target.url,
        "title": title,
        "content": html,
    }


async def extract(page: Page, url: str, wait_until: str, unwrap: bool, wait_for: str | None,
                  wait_timeout_ms: int, delay_ms: int, timeout_s: int,
                  selectors: dict[str, str], max_results: int) -> dict[str, Any]:
    resp = await page.goto(url, wait_until=wait_until, timeout=timeout_s * 1000)
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)
    target, _ = await _resolve_target(page, unwrap, _settle_budget(timeout_s))
    await _await_frame_ready(target, wait_for, wait_timeout_ms)
    data: dict[str, list[str]] = {}
    for name, sel in selectors.items():
        data[name] = await _eval_texts(target, sel, max_results)
    return {
        "url": url,
        "status": resp.status if resp else 0,
        "final_url": page.url,
        "content_url": target.url,
        "data": data,
    }


async def _expand_wrapper_iframe(page: Page) -> bool:
    """Grow the wrapper iframe to its full content height so a full_page
    screenshot captures the real page. Same-origin, safe to read."""
    try:
        return await page.main_frame.evaluate(
            """() => {
                const ifr = document.querySelector('iframe[src*="/sttcs/"], iframe');
                if (!ifr) return false;
                const doc = ifr.contentDocument;
                if (!doc) return false;
                const html = doc.documentElement;
                ifr.style.height = html.scrollHeight + 'px';
                ifr.style.width = '100%';
                ifr.style.position = 'relative';
                ifr.style.maxHeight = 'none';
                if (doc.body) doc.body.style.overflow = 'visible';
                if (html) html.style.overflow = 'visible';
                const top = ifr.ownerDocument.documentElement;
                top.style.overflow = 'visible';
                return true;
            }"""
        )
    except Exception:
        return False


async def screenshot(page: Page, url: str, wait_until: str, unwrap: bool, wait_for: str | None,
                     wait_timeout_ms: int, delay_ms: int, timeout_s: int,
                     fmt: str, quality: int, full_page: bool) -> tuple[bytes, str, int]:
    resp = await page.goto(url, wait_until=wait_until, timeout=timeout_s * 1000)
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)
    target, _ = await _resolve_target(page, unwrap, _settle_budget(timeout_s))
    await _await_frame_ready(target, wait_for, wait_timeout_ms)

    want_full = full_page
    if want_full and unwrap and target != page.main_frame:
        want_full = await _expand_wrapper_iframe(page)

    media = "image/jpeg" if fmt == "jpeg" else "image/png"
    opts: dict[str, Any] = {"type": fmt}
    if fmt == "jpeg":
        opts["quality"] = quality
    if want_full:
        opts["full_page"] = True
    buf = await page.screenshot(**opts)
    return buf, media, resp.status if resp else 0
