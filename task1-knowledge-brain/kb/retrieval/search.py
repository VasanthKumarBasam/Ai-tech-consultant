"""Retrieval: vector, keyword (BM25), or hybrid via Reciprocal Rank Fusion."""
from __future__ import annotations

from dataclasses import dataclass

from .store import ChunkStore

RRF_K = 60


@dataclass
class Evidence:
    chunk_id: str
    url: str
    title: str
    section: str
    kind: str
    text: str
    image_url: str | None
    score: float            # fused rank score (mode-dependent scale)
    similarity: float       # cosine similarity query<->chunk (comparable across modes)
    expert_id: int | None = None


def default_min_similarity(embedder_name: str) -> float:
    """Abstention gate: below this best-chunk cosine we don't call the LLM.

    Conservative defaults chosen so the gate only removes clearly off-topic
    questions; tune with `python -m kb.cli calibrate` / EVALS.md.
    """
    if embedder_name.startswith("fastembed:BAAI/bge"):
        return 0.55
    if embedder_name.startswith("voyage"):
        return 0.30
    return 0.05  # hash embedder: lexical overlap only


def retrieve(store: ChunkStore, query: str, *, mode: str = "hybrid", top_k: int = 8, candidate_k: int = 30,
             max_per_article: int = 3) -> list[Evidence]:
    ranked: dict[str, float] = {}
    if mode in ("vector", "hybrid"):
        for rank, (cid, _sim) in enumerate(store.vector_search(query, candidate_k)):
            ranked[cid] = ranked.get(cid, 0.0) + (1.0 / (RRF_K + rank + 1) if mode == "hybrid" else _sim)
    if mode in ("keyword", "hybrid"):
        for rank, (cid, bm) in enumerate(store.keyword_search(query, candidate_k)):
            ranked[cid] = ranked.get(cid, 0.0) + (1.0 / (RRF_K + rank + 1) if mode == "hybrid" else bm)
    if mode not in ("vector", "keyword", "hybrid"):
        raise ValueError(f"unknown retrieval mode {mode}")
    if not ranked:
        return []
    order = sorted(ranked, key=lambda c: -ranked[c])
    rows = store.get(order)
    sims = store.similarity(query, order)
    expired = _expired_expert_ids(store, [rows[c]["expert_id"] for c in order if c in rows and rows[c]["expert_id"]])
    out: list[Evidence] = []
    per_article: dict[str, int] = {}
    for cid in order:
        r = rows.get(cid)
        if not r or (r["expert_id"] and r["expert_id"] in expired):
            continue
        n = per_article.get(r["url"], 0)
        if n >= max_per_article:
            continue
        per_article[r["url"]] = n + 1
        out.append(
            Evidence(
                chunk_id=cid, url=r["url"], title=r["title"], section=r["section"] or "", kind=r["kind"],
                text=r["text"], image_url=r["image_url"], score=round(ranked[cid], 6),
                similarity=round(sims.get(cid, 0.0), 4), expert_id=r["expert_id"],
            )
        )
        if len(out) >= top_k:
            break
    return out


def _expired_expert_ids(store: ChunkStore, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    rows = store.db.query(
        f"SELECT id FROM expert_knowledge WHERE id IN ({marks}) AND (is_active=0 OR (expires_at IS NOT NULL AND expires_at < datetime('now')))",
        tuple(ids),
    )
    return {r["id"] for r in rows}
