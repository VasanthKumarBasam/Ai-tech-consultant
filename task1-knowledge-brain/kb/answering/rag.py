"""Question answering: retrieve -> relevance gate -> Claude (forced tool) -> validate -> cite/escalate."""
from __future__ import annotations

import json
import re
import time
import uuid
from html import escape

from ..config import Settings
from ..db import Database
from ..escalations import Escalations
from ..llm import LLMError
from ..retrieval.search import Evidence, default_min_similarity, retrieve
from ..retrieval.store import ChunkStore
from .prompts import ANSWER_PROMPT_VERSION, ANSWER_SCHEMA, ANSWER_SYSTEM

CITE_RX = re.compile(r"\[(S\d+)\]")


def _evidence_block(evidence: list[Evidence]) -> str:
    parts = []
    for i, e in enumerate(evidence, 1):
        attrs = f'id="S{i}" kind="{e.kind}" title="{escape(e.title, quote=True)}" section="{escape(e.section, quote=True)}"'
        parts.append(f"<source {attrs}>\n{e.text}\n</source>")
    return "<evidence>\n" + "\n".join(parts) + "\n</evidence>"


class QueryService:
    def __init__(self, settings: Settings, db: Database, store: ChunkStore, llm, escalations: Escalations):
        self.s = settings
        self.db = db
        self.store = store
        self.llm = llm
        self.escalations = escalations

    @property
    def min_similarity(self) -> float:
        return self.s.min_similarity if self.s.min_similarity is not None else default_min_similarity(self.store.embedder.name)

    def answer(self, question: str, *, context: str | None = None, top_k: int | None = None, mode: str | None = None,
               model: str | None = None, create_escalation: bool = True, requester: str | None = None,
               request_id: str | None = None) -> dict:
        t0 = time.perf_counter()
        request_id = request_id or uuid.uuid4().hex[:12]
        mode = mode or self.s.retrieval_mode
        model = model or self.s.answer_model
        top_k = top_k or self.s.top_k
        search_text = question if not context else f"{question}\n{context[:500]}"
        evidence = retrieve(self.store, search_text, mode=mode, top_k=top_k, candidate_k=self.s.candidate_k)
        top_sim = max((e.similarity for e in evidence), default=0.0)

        base = {
            "request_id": request_id,
            "question": question,
            "prompt_version": ANSWER_PROMPT_VERSION,
            "model": model,
            "retrieval": {"mode": mode, "top_similarity": round(top_sim, 4), "min_similarity": self.min_similarity,
                          "candidates": len(evidence)},
            "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
        }

        if not evidence or top_sim < self.min_similarity:
            result = {**base, "status": "insufficient_evidence", "answer": (
                "I couldn't find anything in the knowledge base that answers this question, so I've passed it to a human expert."),
                "sources": [], "images": [], "unsupported_points": [question],
                "reason": "no retrieved source is relevant enough (below similarity gate)"}
            return self._finish(result, t0, evidence, create_escalation, context, requester)

        user = (
            f"<question>\n{question}\n</question>\n"
            + (f"<customer_context>\n{context}\n</customer_context>\n" if context else "")
            + _evidence_block(evidence)
            + "\nAnswer the question using only the evidence, then call submit_answer."
        )
        n = len(evidence)

        def validate(d: dict) -> str | None:
            if d.get("status") not in {"answered", "partial", "insufficient_evidence"}:
                return "status must be answered, partial or insufficient_evidence."
            ids = {c.get("source_id") for c in d.get("citations", []) if isinstance(c, dict)}
            ids |= set(CITE_RX.findall(d.get("answer", "")))
            bad = [i for i in ids if not re.fullmatch(r"S\d+", str(i)) or not 1 <= int(str(i)[1:]) <= n]
            if bad:
                return f"citations reference sources that do not exist: {bad}. Valid ids are S1..S{n}."
            if d["status"] in {"answered", "partial"} and not ids:
                return "an answered/partial status requires at least one citation to a provided source."
            return None

        try:
            res = self.llm.structured(
                model=model, system=ANSWER_SYSTEM, content=user, tool_name="submit_answer",
                tool_description="Submit the grounded answer with citations.", schema=ANSWER_SCHEMA,
                max_tokens=self.s.answer_max_tokens, validate=validate,
            )
        except LLMError as e:
            result = {**base, "status": "error", "answer": "", "sources": self._sources(evidence, set()),
                      "images": [], "error": str(e), "reason": "answer generation failed"}
            return self._finish(result, t0, evidence, False, context, requester)

        d = res.data
        cited = {c["source_id"] for c in d.get("citations", []) if isinstance(c, dict) and c.get("source_id")}
        cited |= set(CITE_RX.findall(d.get("answer", "")))
        status = d["status"]
        quotes = {c["source_id"]: c.get("supporting_quote") for c in d.get("citations", []) if isinstance(c, dict)}
        sources = self._sources(evidence, cited, quotes)
        result = {
            **base,
            "status": status,
            "answer": d.get("answer", "").strip(),
            "sources": sources,
            "images": [s for s in sources if s.get("image_url")],
            "unsupported_points": d.get("unsupported_points") or [],
            "reason": d.get("escalation_reason"),
            "usage": {"input_tokens": res.usage.input_tokens, "output_tokens": res.usage.output_tokens,
                      "cost_usd": res.usage.cost_usd, "validation_retries": res.usage.retries},
        }
        return self._finish(result, t0, evidence, create_escalation and status != "answered", context, requester)

    @staticmethod
    def _sources(evidence: list[Evidence], cited: set[str], quotes: dict | None = None) -> list[dict]:
        """Cited sources first (in citation order), then the remaining retrieved ones flagged cited=false."""
        out = []
        for i, e in enumerate(evidence, 1):
            sid = f"S{i}"
            out.append({
                "id": sid, "cited": sid in cited, "title": e.title, "url": e.url, "section": e.section, "kind": e.kind,
                "image_url": e.image_url if e.kind == "image" else None, "similarity": e.similarity,
                "snippet": e.text[:300], "supporting_quote": (quotes or {}).get(sid), "chunk_id": e.chunk_id,
            })
        return sorted(out, key=lambda s: not s["cited"])

    def _finish(self, result: dict, t0: float, evidence: list[Evidence], escalate: bool, context: str | None,
                requester: str | None) -> dict:
        result["needs_human"] = result["status"] != "answered"
        result["escalation_id"] = None
        if escalate and result["status"] in {"insufficient_evidence", "partial"}:
            esc = self.escalations.create(
                result["question"], reason=result.get("reason") or result["status"], context=context,
                evidence=[{"title": e.title, "url": e.url, "similarity": e.similarity} for e in evidence[:5]],
                requester=requester,
            )
            result["escalation_id"] = esc["id"]
        result["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        u = result["usage"]
        self.db.execute(
            """INSERT INTO query_log(request_id,question,status,sources_json,model,prompt_version,retrieval_mode,top_similarity,
                                     latency_ms,input_tokens,output_tokens,cost_usd,escalation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (result["request_id"], result["question"], result["status"],
             json.dumps([s["url"] for s in result.get("sources", []) if s.get("cited")]), result["model"],
             result["prompt_version"], result["retrieval"]["mode"], result["retrieval"]["top_similarity"],
             result["latency_ms"], u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cost_usd", 0.0),
             result["escalation_id"]),
        )
        return result
