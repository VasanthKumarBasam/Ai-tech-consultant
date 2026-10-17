from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb.config import Settings  # noqa: E402
from kb.ingestion.crawler import Fetcher  # noqa: E402
from kb.llm import FakeLLM  # noqa: E402
from kb.services import build_services  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
HOST = "https://support.example.test"


def tiny_png() -> bytes:
    raw = b"\x00\xff\x00\x00" * 4
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeSite:
    """In-memory help centre served through httpx.MockTransport."""

    def __init__(self):
        self.sitemap = (FIX / "sitemap.xml").read_text()
        self.pages: dict[str, str] = {}
        for f in FIX.glob("*.html"):
            self.pages[f"{HOST}/support/solutions/articles/{f.stem}"] = f.read_text()
        self.robots = "User-agent: *\nDisallow: /support/search\n"
        self.fail_plan: dict[str, list[int]] = {}   # url -> statuses to return before succeeding
        self.hits: dict[str, int] = {}
        self.etag_support = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.hits[url] = self.hits.get(url, 0) + 1
        plan = self.fail_plan.get(url)
        if plan:
            status = plan.pop(0)
            if status == -1:
                raise httpx.ConnectError("simulated network failure", request=request)
            return httpx.Response(status, headers={"Retry-After": "0"})
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=self.robots)
        if url.endswith("/sitemap.xml"):
            return httpx.Response(200, text=self.sitemap)
        if "s3.amazonaws.com" in url:
            return httpx.Response(200, content=tiny_png(), headers={"content-type": "image/png"})
        if url in self.pages:
            body = self.pages[url]
            etag = f'"{hash(body) & 0xFFFFFFFF:x}"'
            if self.etag_support and request.headers.get("if-none-match") == etag:
                return httpx.Response(304, headers={"etag": etag})
            return httpx.Response(200, text=body, headers={"etag": etag, "content-type": "text/html"})
        return httpx.Response(404, text="not found")


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "kb.sqlite3",
        escalated_cases_path=tmp_path / "escalated_cases.jsonl",
        sitemap_url=f"{HOST}/support/sitemap.xml",
        embedder="hash",
        llm_provider="fake",
        crawl_delay_s=0.0,
        image_fetch_delay_s=0.0,
        backoff_base_s=0.01,
        max_retries=2,
        image_min_bytes=10,
        chunk_min_chars=120,
        min_similarity=0.05,
    )


@pytest.fixture
def services(settings, site):
    client = httpx.AsyncClient(transport=httpx.MockTransport(site.handler), headers={"User-Agent": settings.user_agent})

    fetcher = Fetcher(settings, client=client)
    return build_services(settings, llm=FakeLLM(settings), fetcher=fetcher)


@pytest.fixture
def fresh_fetcher(settings, site):
    """Return a factory: a new Fetcher (new robots cache, new limiter) per run."""
    def make():
        client = httpx.AsyncClient(transport=httpx.MockTransport(site.handler))
        return Fetcher(settings, client=client)
    return make
