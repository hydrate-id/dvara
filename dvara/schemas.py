from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

WaitUntil = Literal["load", "domcontentloaded", "networkidle"]


class Proxy(BaseModel):
    server: str
    username: str | None = None
    password: str | None = None


class _BaseJob(BaseModel):
    url: str
    wait_until: WaitUntil = "load"
    wait_for: str | None = None
    wait_timeout_ms: int = Field(default=15_000, ge=500, le=120_000)
    delay_ms: int = Field(default=0, ge=0, le=60_000)
    session_id: str | None = None
    proxy: Proxy | str | None = None
    unwrap: bool = True
    timeout_s: int = Field(default=0, ge=5, le=300)  # 0 = use config default

    @model_validator(mode="after")
    def _check_url(self) -> "_BaseJob":
        # Manual check: HttpUrl rejects URLs that real-world sites use
        if not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise ValueError("url must be http(s)")
        return self


class RenderRequest(_BaseJob):
    response_format: Literal["html", "json"] = "html"


class ExtractRequest(_BaseJob):
    selectors: dict[str, str] = Field(min_length=1)
    max_results: int = Field(default=100, ge=1, le=500)


class ScreenshotRequest(_BaseJob):
    format: Literal["png", "jpeg"] = "png"
    quality: int = Field(default=85, ge=1, le=100)
    full_page: bool = False


class JobResult(BaseModel):
    url: str
    status: int = 200
    final_url: str = ""
    content_url: str = ""
    title: str = ""
    content: str = ""


class ExtractResult(BaseModel):
    url: str
    status: int = 200
    final_url: str = ""
    content_url: str = ""
    data: dict[str, list[str]]


def http_error(status_code: int, code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


# query params aliases (GET usage)
RENDER_QUERY = {
    "url": str,
    "wait_until": WaitUntil,
    "wait_for": str | None,
    "wait_timeout_ms": int | None,
    "delay_ms": int | None,
    "session_id": str | None,
    "proxy": str | None,
    "unwrap": bool | None,
    "timeout_s": int | None,
    "response_format": str | None,
}

EXTRACT_QUERY = {
    "url": str,
    "wait_until": WaitUntil,
    "wait_for": str | None,
    "delay_ms": int | None,
    "session_id": str | None,
    "proxy": str | None,
    "unwrap": bool | None,
    "timeout_s": int | None,
}

SCREENSHOT_QUERY = {
    "url": str,
    "wait_until": WaitUntil,
    "wait_for": str | None,
    "delay_ms": int | None,
    "session_id": str | None,
    "proxy": str | None,
    "unwrap": bool | None,
    "timeout_s": int | None,
    "format": str | None,
    "quality": int | None,
    "full_page": bool | None,
}
