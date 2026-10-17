from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scope.api import build_services, create_app  # noqa: E402
from scope.config import Settings  # noqa: E402
from scope.fake_llm import FakeLLM  # noqa: E402
from scope.kb_client import KnowledgeClient  # noqa: E402

TRANSCRIPTS = Path(__file__).resolve().parents[1] / "transcripts"
SEARCH_URL = "https://support.brilliantdirectories.com/support/solutions/articles/5000681551-search-layouts-the-difference-between-list-grid-and-map-view"


class FakeKB:
    """Mock Knowledge Brain: answers anything mentioning map/search/sort/payment, otherwise 'insufficient'."""

    def __init__(self, mode: str = "ok"):
        self.mode = mode
        self.questions: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "down":
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "articles_active": 4})
        q = json.loads(request.content)["question"]
        self.questions.append(q)
        hit = any(w in q.lower() for w in ("map", "sort", "distance", "stripe", "payment"))
        return httpx.Response(200, json={
            "status": "answered" if hit else "insufficient_evidence",
            "answer": "Map View needs the Google Map Search Results add-on. [S1]" if hit else "Not documented.",
            "sources": [{"id": "S1", "cited": True, "title": "Search Layouts", "url": SEARCH_URL, "section": "x", "kind": "text",
                         "similarity": 0.8, "snippet": "...", "chunk_id": "c1"}] if hit else [],
            "escalation_id": None if hit else 3, "usage": {"cost_usd": 0.001}, "latency_ms": 5,
        })


@pytest.fixture
def kb():
    return FakeKB()


@pytest.fixture
def svc(tmp_path, kb):
    s = Settings(db_path=tmp_path / "scope.sqlite3", sent_dir=tmp_path / "sent", llm_provider="fake", chunk_max_chars=4000)
    client = KnowledgeClient("http://kb.test", transport=httpx.MockTransport(kb.handler), retries=0)
    return build_services(s, llm=FakeLLM(), kb=client)


@pytest.fixture
def client(svc):
    from fastapi.testclient import TestClient

    return TestClient(create_app(svc))


def transcript(name: str) -> str:
    return (TRANSCRIPTS / name).read_text()
