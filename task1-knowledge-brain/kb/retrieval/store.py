"""Chunk store: SQLite rows + FTS5 keyword index + in-memory numpy vector index.

The vector matrix is rebuilt lazily whenever `meta.index_version` changes, so
an ingestion run in another process (CLI / scheduler) becomes visible to the
API on the next query without a restart.
"""
from __future__ import annotations

import json
import re
import threading

import numpy as np

from ..db import Database
from .embeddings import Embedder

STOPWORDS = set(
    "a an and are as at be but by can do does for from how i if in into is it its me my of on or our so that the their "
    "then there these they this to was we what when where which who why will with you your please hi hello thanks".split()
)


class EmbedderMismatch(RuntimeError):
    pass


class ChunkStore:
    def __init__(self, db: Database, embedder: Embedder, allow_mismatch: bool = False):
        self.db = db
        self.embedder = embedder
        self._lock = threading.Lock()
        self._loaded_version: str | None = None
        self._ids: list[str] = []
        self._matrix = np.zeros((0, embedder.dim), dtype=np.float32)
        stored = db.get_meta("embedder")
        has_chunks = db.one("SELECT 1 FROM chunks WHERE embedding IS NOT NULL LIMIT 1") is not None
        if stored and stored != embedder.name and has_chunks and not allow_mismatch:
            raise EmbedderMismatch(
                f"Index was built with {stored} but KB_EMBEDDER is {embedder.name}. "
                "Run `python -m kb.cli reindex` (or switch KB_EMBEDDER back)."
            )
        if not stored or not has_chunks:
            db.set_meta("embedder", embedder.name)

    # writes ---------------------------------------------------------------
    def _bump(self) -> None:
        v = int(self.db.get_meta("index_version") or "0") + 1
        self.db.set_meta("index_version", str(v))

    def add_chunks(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        vecs = self.embedder.embed_documents([r["embed_text"] for r in rows])
        with self.db.connect() as conn:
            for r, v in zip(rows, vecs):
                conn.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (r["chunk_id"],))
                conn.execute(
                    """INSERT INTO chunks(chunk_id,version_id,expert_id,url,article_id,title,section,kind,text,image_url,
                                          image_refs_json,embedding,dim,is_active)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(chunk_id) DO UPDATE SET text=excluded.text, embedding=excluded.embedding,
                           dim=excluded.dim, is_active=1, section=excluded.section, title=excluded.title,
                           image_url=excluded.image_url, image_refs_json=excluded.image_refs_json""",
                    (
                        r["chunk_id"], r.get("version_id"), r.get("expert_id"), r["url"], r.get("article_id"),
                        r["title"], r.get("section"), r["kind"], r["text"], r.get("image_url"),
                        json.dumps(r.get("image_refs", [])), np.asarray(v, dtype=np.float32).tobytes(), int(len(v)),
                    ),
                )
                conn.execute(
                    "INSERT INTO chunks_fts(chunk_id,title,section,text) VALUES(?,?,?,?)",
                    (r["chunk_id"], r["title"], r.get("section") or "", r["text"]),
                )
        self._bump()
        return len(rows)

    def set_active(self, where: str, params: tuple, active: bool) -> int:
        with self.db.connect() as conn:
            rows = list(conn.execute(f"SELECT chunk_id,title,section,text FROM chunks WHERE {where}", params))
            conn.execute(f"UPDATE chunks SET is_active=? WHERE {where}", (int(active), *params))
            for r in rows:
                conn.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (r["chunk_id"],))
                if active:
                    conn.execute(
                        "INSERT INTO chunks_fts(chunk_id,title,section,text) VALUES(?,?,?,?)",
                        (r["chunk_id"], r["title"], r["section"] or "", r["text"]),
                    )
        self._bump()
        return len(rows)

    def reindex(self) -> int:
        """Re-embed every chunk with the current embedder (after changing KB_EMBEDDER)."""
        rows = self.db.query("SELECT chunk_id,title,section,text FROM chunks")
        for i in range(0, len(rows), 256):
            batch = rows[i : i + 256]
            vecs = self.embedder.embed_documents([f"{r['title']} > {r['section']}\n{r['text']}" for r in batch])
            with self.db.connect() as conn:
                for r, v in zip(batch, vecs):
                    conn.execute(
                        "UPDATE chunks SET embedding=?, dim=? WHERE chunk_id=?",
                        (np.asarray(v, dtype=np.float32).tobytes(), int(len(v)), r["chunk_id"]),
                    )
        self.db.set_meta("embedder", self.embedder.name)
        self._bump()
        return len(rows)

    # reads ----------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        version = self.db.get_meta("index_version") or "0"
        if version == self._loaded_version:
            return
        with self._lock:
            rows = self.db.query("SELECT chunk_id, embedding FROM chunks WHERE is_active=1 AND embedding IS NOT NULL")
            self._ids = [r["chunk_id"] for r in rows]
            if rows:
                self._matrix = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
            else:
                self._matrix = np.zeros((0, self.embedder.dim), dtype=np.float32)
            self._loaded_version = version

    def vector_search(self, query: str, k: int) -> list[tuple[str, float]]:
        self._ensure_loaded()
        if not self._ids:
            return []
        q = self.embedder.embed_query(query)
        sims = self._matrix @ q
        k = min(k, len(self._ids))
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        return [(self._ids[i], float(sims[i])) for i in idx]

    def similarity(self, query: str, chunk_ids: list[str]) -> dict[str, float]:
        self._ensure_loaded()
        pos = {cid: i for i, cid in enumerate(self._ids)}
        q = self.embedder.embed_query(query)
        return {c: float(self._matrix[pos[c]] @ q) for c in chunk_ids if c in pos}

    @staticmethod
    def fts_query(text: str) -> str | None:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", text.lower()) if w not in STOPWORDS and len(w) > 1]
        if not words:
            return None
        return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))

    def keyword_search(self, query: str, k: int) -> list[tuple[str, float]]:
        q = self.fts_query(query)
        if not q:
            return []
        rows = self.db.query(
            """SELECT f.chunk_id, bm25(chunks_fts, 0.0, 2.0, 1.5, 1.0) AS score
               FROM chunks_fts f JOIN chunks c ON c.chunk_id=f.chunk_id
               WHERE chunks_fts MATCH ? AND c.is_active=1
               ORDER BY score LIMIT ?""",
            (q, k),
        )
        return [(r["chunk_id"], -float(r["score"])) for r in rows]

    def get(self, chunk_ids: list[str]) -> dict[str, dict]:
        if not chunk_ids:
            return {}
        marks = ",".join("?" * len(chunk_ids))
        rows = self.db.query(
            f"SELECT chunk_id,url,article_id,title,section,kind,text,image_url,image_refs_json,version_id,expert_id "
            f"FROM chunks WHERE chunk_id IN ({marks})",
            tuple(chunk_ids),
        )
        return {r["chunk_id"]: dict(r) for r in rows}

    def counts(self) -> dict:
        r = self.db.one(
            "SELECT COUNT(*) n, SUM(kind='text') t, SUM(kind='image') i, SUM(kind='expert') e FROM chunks WHERE is_active=1"
        )
        return {"chunks_active": r["n"] or 0, "text": r["t"] or 0, "image": r["i"] or 0, "expert": r["e"] or 0}
