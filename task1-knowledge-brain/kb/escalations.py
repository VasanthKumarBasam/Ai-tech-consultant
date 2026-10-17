"""Human-expert escalations and the expert-knowledge records they produce.

An expert answer is stored as a separate, versioned knowledge record — never
written into the scraped articles. Only answers marked scope="general" are
indexed for retrieval (customer-specific answers stay on the ticket), and they
can carry an expiry date. Every resolved question is appended to the eval set
so the regression suite grows with real gaps.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .db import Database
from .retrieval.store import ChunkStore


def normalize_question(q: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", q.lower())).strip()


class Escalations:
    def __init__(self, db: Database, store: ChunkStore, cases_path: Path | None = None):
        self.db = db
        self.store = store
        self.cases_path = cases_path

    def create(self, question: str, reason: str, context: str | None = None, evidence: list[dict] | None = None,
               requester: str | None = None) -> dict:
        norm = normalize_question(question)
        existing = self.db.one("SELECT * FROM escalations WHERE normalized_question=? AND status='open'", (norm,))
        if existing:
            return {**dict(existing), "deduplicated": True}
        eid = self.db.execute(
            "INSERT INTO escalations(question,normalized_question,context,reason,evidence_json,requester) VALUES(?,?,?,?,?,?)",
            (question, norm, context, reason, json.dumps(evidence or []), requester),
        )
        return {**dict(self.db.one("SELECT * FROM escalations WHERE id=?", (eid,))), "deduplicated": False}

    def list(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.db.query("SELECT * FROM escalations WHERE status=? ORDER BY id DESC", (status,))
        else:
            rows = self.db.query("SELECT * FROM escalations ORDER BY id DESC")
        return [dict(r) for r in rows]

    def get(self, eid: int) -> dict | None:
        row = self.db.one("SELECT * FROM escalations WHERE id=?", (eid,))
        return dict(row) if row else None

    def resolve(self, eid: int, answer: str, resolved_by: str, scope: str = "general",
                expires_at: str | None = None, supporting_url: str | None = None, add_to_knowledge: bool = True) -> dict:
        esc = self.get(eid)
        if not esc:
            raise KeyError(f"escalation {eid} not found")
        if esc["status"] != "open":
            raise ValueError(f"escalation {eid} is already {esc['status']}")
        expert_id = None
        if add_to_knowledge:
            prev = self.db.one(
                "SELECT MAX(version) v FROM expert_knowledge e JOIN escalations s ON s.id=e.escalation_id WHERE s.normalized_question=?",
                (esc["normalized_question"],),
            )
            version = (prev["v"] or 0) + 1
            # older expert answers to the same question are superseded
            for old in self.db.query(
                "SELECT e.id FROM expert_knowledge e JOIN escalations s ON s.id=e.escalation_id WHERE s.normalized_question=? AND e.is_active=1",
                (esc["normalized_question"],),
            ):
                self.db.execute("UPDATE expert_knowledge SET is_active=0 WHERE id=?", (old["id"],))
                self.store.set_active("expert_id=?", (old["id"],), False)
            expert_id = self.db.execute(
                """INSERT INTO expert_knowledge(escalation_id,question,answer,supporting_url,approved_by,scope,version,expires_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (eid, esc["question"], answer, supporting_url, resolved_by, scope, version, expires_at),
            )
            if scope == "general":
                text = f"Expert-approved answer (v{version}, approved by {resolved_by}).\nQuestion: {esc['question']}\nAnswer: {answer}"
                if supporting_url:
                    text += f"\nSupporting source: {supporting_url}"
                self.store.add_chunks([{
                    "chunk_id": f"expert:{expert_id}", "expert_id": expert_id, "url": f"kb://expert/{expert_id}",
                    "title": f"Expert answer: {esc['question'][:80]}", "section": "Expert knowledge", "kind": "expert",
                    "text": text, "embed_text": f"{esc['question']}\n{answer}",
                }])
        self.db.execute(
            """UPDATE escalations SET status='resolved', resolved_at=datetime('now'), resolved_by=?, answer=?, scope=?,
                   expires_at=?, expert_id=? WHERE id=?""",
            (resolved_by, answer, scope, expires_at, expert_id, eid),
        )
        self._append_eval_case(self.cases_path, esc, answer, scope)
        return self.get(eid)

    def dismiss(self, eid: int, resolved_by: str, note: str = "") -> dict:
        self.db.execute(
            "UPDATE escalations SET status='dismissed', resolved_at=datetime('now'), resolved_by=?, answer=? WHERE id=? AND status='open'",
            (resolved_by, note, eid),
        )
        return self.get(eid)

    @staticmethod
    def _append_eval_case(path: Path | None, esc: dict, answer: str, scope: str) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps({
                    "id": f"ESC-{esc['id']}", "question": esc["question"], "type": "escalated",
                    "expected_status": "answered" if scope == "general" else "insufficient_evidence",
                    "reference_answer": answer, "added_at": datetime.now(timezone.utc).isoformat(),
                }) + "\n")
        except OSError:
            pass
