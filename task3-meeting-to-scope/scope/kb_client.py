"""HTTP client for the Knowledge Brain (Task 1). The only coupling between Task 3 and Task 1."""
from __future__ import annotations

import time

import httpx


class KnowledgeUnavailable(RuntimeError):
    pass


class KnowledgeClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout_s: float = 60, retries: int = 1,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        headers = {"X-API-Key": api_key} if api_key else {}
        self.http = httpx.Client(base_url=self.base_url, timeout=timeout_s, headers=headers, transport=transport)

    def _post(self, path: str, payload: dict) -> dict:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.http.post(path, json=payload)
                if r.status_code >= 500:
                    last = KnowledgeUnavailable(f"Knowledge Brain returned HTTP {r.status_code}")
                elif r.status_code >= 400:
                    raise ValueError(f"Knowledge Brain rejected the request: {r.text[:300]}")
                else:
                    return r.json()
            except httpx.HTTPError as e:
                last = KnowledgeUnavailable(f"Knowledge Brain unavailable: {type(e).__name__}: {e}")
            if attempt < self.retries:
                time.sleep(0.5 * (attempt + 1))
        raise last or KnowledgeUnavailable("Knowledge Brain unavailable")

    def query(self, question: str, context: str | None = None, request_id: str | None = None) -> dict:
        body = self._post("/query", {"question": question[:2000], "context": (context or "")[:6000] or None,
                                     "requester": "meeting-to-scope"})
        for key in ("status", "answer", "sources"):
            if key not in body:
                raise KnowledgeUnavailable(f"Knowledge Brain response broke the contract (missing {key})")
        return body

    def health(self) -> dict:
        try:
            r = self.http.get("/health", timeout=5)
            return {"ok": r.status_code == 200, **(r.json() if r.status_code == 200 else {})}
        except httpx.HTTPError as e:
            return {"ok": False, "error": str(e)}
