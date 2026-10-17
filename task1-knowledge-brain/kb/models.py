"""Public API contract (request/response models). See shared-docs/kb-api.md."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=2000, description="The customer's or agent's question.")
    context: str | None = Field(None, max_length=6000, description="Optional extra context (e.g. the email it came from). Used for retrieval only.")
    top_k: int | None = Field(None, ge=1, le=20)
    retrieval_mode: Literal["vector", "keyword", "hybrid"] | None = None
    model: str | None = Field(None, description="Override the answer model (used by evals).")
    create_escalation: bool = Field(True, description="Open a human-expert task when the answer is not fully supported.")
    requester: str | None = Field(None, max_length=100, description="Calling system, e.g. 'inbox-agent'.")


class Source(BaseModel):
    id: str
    cited: bool
    title: str
    url: str
    section: str
    kind: Literal["text", "image", "expert"]
    image_url: str | None = None
    similarity: float
    snippet: str
    supporting_quote: str | None = None
    chunk_id: str


class Retrieval(BaseModel):
    mode: str
    top_similarity: float
    min_similarity: float
    candidates: int


class QueryResponse(BaseModel):
    request_id: str
    question: str
    status: Literal["answered", "partial", "insufficient_evidence", "error"]
    answer: str
    sources: list[Source]
    images: list[Source]
    needs_human: bool
    escalation_id: int | None
    unsupported_points: list[str] = []
    reason: str | None = None
    model: str
    prompt_version: str
    retrieval: Retrieval
    usage: dict
    latency_ms: int
    error: str | None = None


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=2000)
    top_k: int = Field(8, ge=1, le=50)
    retrieval_mode: Literal["vector", "keyword", "hybrid"] = "hybrid"


class IngestionRunRequest(BaseModel):
    mode: Literal["incremental", "full", "rebuild"] = "incremental"
    limit: int | None = Field(None, ge=1, description="Pilot crawl: only the first N article URLs (no removals).")
    urls: list[str] | None = Field(None, description="Only (re)ingest these article URLs.")


class EscalationCreate(BaseModel):
    question: str = Field(..., min_length=3, max_length=2000)
    reason: str = Field("manual escalation", max_length=500)
    context: str | None = Field(None, max_length=6000)
    requester: str | None = None


class EscalationResolve(BaseModel):
    answer: str = Field(..., min_length=3, max_length=8000)
    resolved_by: str = Field(..., min_length=1, max_length=100)
    scope: Literal["general", "customer_specific"] = Field(
        "general", description="general = reusable product fact (indexed); customer_specific = only for this ticket."
    )
    expires_at: str | None = Field(None, description="ISO date after which the answer is no longer served.")
    supporting_url: str | None = None
    add_to_knowledge: bool = True


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
