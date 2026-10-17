"""Polite async HTTP fetcher.

* bounded concurrency (semaphore) + a global minimum delay between requests
* exponential backoff with jitter for 429 / 5xx / network errors, honours Retry-After
* permanent failures (404, 410, 401, 403) are not retried
* conditional GET (ETag / Last-Modified) so unchanged pages cost a 304
* robots.txt is checked before every page fetch
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urlparse

import httpx

from ..config import Settings

log = logging.getLogger("kb.crawler")

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
PERMANENT = {400, 401, 403, 404, 410}


@dataclass
class FetchResult:
    url: str
    status: int | None
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    permanent: bool = False
    not_modified: bool = False
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class RateLimiter:
    """Ensures at least `delay` seconds between request starts across all workers."""

    def __init__(self, delay: float):
        self.delay = delay
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
            self._next = time.monotonic() + self.delay


class Fetcher:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )
        self.sem = asyncio.Semaphore(max(1, settings.crawl_concurrency))
        # one limiter per host: the help centre gets the full politeness delay,
        # the attachment CDN (S3) a shorter one.
        self.primary_host = urlparse(settings.sitemap_url).netloc
        self._limiters: dict[str, RateLimiter] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.requests_made = 0

    def limiter(self, url: str) -> RateLimiter:
        host = urlparse(url).netloc
        if host not in self._limiters:
            delay = self.s.crawl_delay_s if host == self.primary_host else self.s.image_fetch_delay_s
            self._limiters[host] = RateLimiter(delay)
        return self._limiters[host]

    async def close(self) -> None:
        if self._own_client:
            await self.client.aclose()

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # robots -------------------------------------------------------------
    async def allowed(self, url: str) -> bool:
        if not self.s.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            res = await self.get(f"{base}/robots.txt", check_robots=False, max_retries=1)
            if res.ok:
                rp.parse(res.text.splitlines())
            elif res.status in (401, 403):
                rp.disallow_all = True  # robots itself forbidden -> be conservative
            else:
                rp = None  # no robots.txt -> allowed
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.s.user_agent, url)

    # fetching -----------------------------------------------------------
    async def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        check_robots: bool = True,
        max_retries: int | None = None,
        max_bytes: int | None = None,
    ) -> FetchResult:
        if check_robots and not await self.allowed(url):
            return FetchResult(url, None, error="disallowed by robots.txt", permanent=True)
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        retries = self.s.max_retries if max_retries is None else max_retries
        attempt = 0
        last_error = None
        while True:
            attempt += 1
            async with self.sem:
                await self.limiter(url).wait()
                self.requests_made += 1
                try:
                    resp = await self.client.get(url, headers=headers)
                    status = resp.status_code
                    content = resp.content
                    if max_bytes and len(content) > max_bytes:
                        return FetchResult(url, status, error=f"too large ({len(content)} bytes)", permanent=True, attempts=attempt)
                    if status == 304:
                        return FetchResult(url, status, headers=dict(resp.headers), not_modified=True, attempts=attempt)
                    if 200 <= status < 300:
                        return FetchResult(url, status, content, dict(resp.headers), attempts=attempt)
                    last_error = f"HTTP {status}"
                    if status in PERMANENT or status not in RETRYABLE:
                        return FetchResult(url, status, error=last_error, permanent=True, attempts=attempt)
                    retry_after = resp.headers.get("Retry-After")
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    status, retry_after = None, None
                    last_error = f"{type(e).__name__}: {e}"
            if attempt > retries:
                return FetchResult(url, status, error=last_error, permanent=False, attempts=attempt)
            delay = self.s.backoff_base_s * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            if retry_after and retry_after.isdigit():
                delay = max(delay, min(float(retry_after), 120.0))
            log.info("retrying %s in %.1fs (%s)", url, delay, last_error)
            await asyncio.sleep(delay)
