from __future__ import annotations

import os


class Settings:
    def __init__(self) -> None:
        self.api_key: str | None = os.getenv("DVARA_API_KEY") or None
        self.host: str = os.getenv("DVARA_HOST", "0.0.0.0")
        self.port: int = int(os.getenv("DVARA_PORT", "8080"))
        self.pool_size: int = int(os.getenv("DVARA_POOL_SIZE", "2"))
        self.max_concurrent: int = int(os.getenv("DVARA_MAX_CONCURRENT", "8"))
        self.request_timeout_s: int = int(os.getenv("DVARA_TIMEOUT_S", "70"))
        self.db_path: str = os.getenv("DVARA_DB", "dvara.db")
        self.headless: bool = os.getenv("DVARA_HEADLESS", "1") != "0"
        self.browser_extra: str | None = os.getenv("DVARA_BROWSER_EXTRA") or None
        # default wait_until used by render/extract/screenshot
        self.wait_until: str = os.getenv("DVARA_WAIT_UNTIL", "load")
        self.default_proxy: str | None = os.getenv("DVARA_DEFAULT_PROXY") or None

    @property
    def goto_timeout_ms(self) -> int:
        return max(1000, (self.request_timeout_s - 3) * 1000)


settings = Settings()
