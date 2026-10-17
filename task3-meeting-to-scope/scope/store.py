"""Persistence: projects, meetings, versioned scope packages, approvals, jobs and an audit trail.

Approval rules (enforced here, not in the UI):
* only a draft can be approved, by a named reviewer; blocking review issues must be acknowledged;
* approval stores the content hash; any later edit drops the version back to draft;
* only an approved version whose content still matches the approved hash can be sent;
* approving a version supersedes the project's previously approved one, which stays in history.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, client TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS meetings (
  id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), title TEXT NOT NULL, meeting_date TEXT,
  kind TEXT NOT NULL, transcript TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL REFERENCES projects(id), number INTEGER NOT NULL,
  kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', payload TEXT NOT NULL, content_hash TEXT NOT NULL,
  based_on INTEGER REFERENCES versions(id), meeting_id TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
  approved_by TEXT, approved_at TEXT, approved_hash TEXT, approval_note TEXT, sent_by TEXT, sent_at TEXT, sent_path TEXT,
  edited_by TEXT, edited_at TEXT, UNIQUE(project_id, number)
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, meeting_id TEXT NOT NULL, status TEXT NOT NULL,
  stage TEXT, stages TEXT NOT NULL DEFAULT '[]', error TEXT, version_id INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL DEFAULT (datetime('now')), actor TEXT NOT NULL, action TEXT NOT NULL,
  entity TEXT NOT NULL, entity_id TEXT, detail TEXT
);
"""


class ApprovalError(RuntimeError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def content_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "meeting"


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def _q(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params)]

    def _x(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    def log(self, actor: str, action: str, entity: str, entity_id, detail=None) -> None:
        self._x("INSERT INTO audit(actor,action,entity,entity_id,detail) VALUES(?,?,?,?,?)",
                (actor, action, entity, None if entity_id is None else str(entity_id), json.dumps(detail) if detail is not None else None))

    def audit(self, limit: int = 200) -> list[dict]:
        return self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # projects / meetings ------------------------------------------------
    def create_project(self, name: str, client: str) -> dict:
        pid = self._x("INSERT INTO projects(name, client) VALUES(?,?)", (name, client))
        self.log("system", "project_created", "project", pid, {"name": name})
        return self.project(pid)

    def project(self, pid: int) -> dict | None:
        rows = self._q("SELECT * FROM projects WHERE id=?", (pid,))
        return rows[0] if rows else None

    def projects(self) -> list[dict]:
        return self._q("""SELECT p.*, (SELECT COUNT(*) FROM meetings m WHERE m.project_id=p.id) meetings,
                                 (SELECT COUNT(*) FROM versions v WHERE v.project_id=p.id) versions FROM projects p ORDER BY p.id""")

    def add_meeting(self, project_id: int, title: str, date: str | None, kind: str, transcript: str, meeting_id: str | None = None) -> dict:
        base = meeting_id or f"{slug(title)}-{date or 'undated'}"
        mid, n = base, 2
        while self._q("SELECT 1 FROM meetings WHERE id=?", (mid,)):
            mid, n = f"{base}-{n}", n + 1
        self._x("INSERT INTO meetings(id, project_id, title, meeting_date, kind, transcript) VALUES(?,?,?,?,?,?)",
                (mid, project_id, title, date, kind, transcript))
        self.log("system", "meeting_added", "meeting", mid, {"project": project_id, "kind": kind})
        return self.meeting(mid)

    def meeting(self, mid: str) -> dict | None:
        rows = self._q("SELECT * FROM meetings WHERE id=?", (mid,))
        return rows[0] if rows else None

    def meetings(self, project_id: int) -> list[dict]:
        return self._q("SELECT id, project_id, title, meeting_date, kind, length(transcript) chars, created_at FROM meetings WHERE project_id=? ORDER BY meeting_date, created_at", (project_id,))

    # versions -----------------------------------------------------------
    def add_version(self, project_id: int, kind: str, payload: dict, based_on: int | None, meeting_id: str | None) -> dict:
        with self._lock:
            n = (self._q("SELECT MAX(number) n FROM versions WHERE project_id=?", (project_id,))[0]["n"] or 0) + 1
            vid = self._x("INSERT INTO versions(project_id, number, kind, payload, content_hash, based_on, meeting_id) VALUES(?,?,?,?,?,?,?)",
                          (project_id, n, kind, json.dumps(payload, default=str), content_hash(payload), based_on, meeting_id))
        self.log("pipeline", "version_created", "version", vid, {"project": project_id, "number": n, "kind": kind})
        return self.version(vid)

    def version(self, vid: int, with_payload: bool = True) -> dict | None:
        rows = self._q("SELECT * FROM versions WHERE id=?", (vid,))
        if not rows:
            return None
        v = rows[0]
        if with_payload:
            v["payload"] = json.loads(v["payload"])
        else:
            v.pop("payload")
        return v

    def versions(self, project_id: int) -> list[dict]:
        rows = self._q("SELECT * FROM versions WHERE project_id=? ORDER BY number", (project_id,))
        for r in rows:
            p = json.loads(r.pop("payload"))
            est = p.get("estimate", {})
            r["summary"] = {"hours": est.get("total_with_contingency"), "price": est.get("price_expected"), "currency": est.get("currency"),
                            "deliverables": len(p.get("scope", {}).get("deliverables", [])),
                            "blocking_issues": sum(1 for i in p.get("review_issues", []) if i["severity"] == "block")}
        return rows

    def latest_approved(self, project_id: int) -> dict | None:
        rows = self._q("SELECT id FROM versions WHERE project_id=? AND status IN ('approved','sent') ORDER BY number DESC LIMIT 1", (project_id,))
        return self.version(rows[0]["id"]) if rows else None

    def update_payload(self, vid: int, payload: dict, editor: str, what: str) -> dict:
        v = self.version(vid)
        if not v:
            raise ApprovalError("version not found", 404)
        if v["status"] in ("sent", "superseded"):
            raise ApprovalError(f"version is {v['status']} and can no longer be edited; create a new version instead")
        was_approved = v["status"] == "approved"
        self._x("""UPDATE versions SET payload=?, content_hash=?, status='draft', approved_by=NULL, approved_at=NULL, approved_hash=NULL,
                   edited_by=?, edited_at=datetime('now') WHERE id=?""",
                (json.dumps(payload, default=str), content_hash(payload), editor, vid))
        self.log(editor, "version_edited", "version", vid, {"what": what, "approval_invalidated": was_approved})
        return self.version(vid)

    def approve(self, vid: int, reviewer: str, note: str | None, acknowledge_blocks: bool) -> dict:
        v = self.version(vid)
        if not v:
            raise ApprovalError("version not found", 404)
        if v["status"] != "draft":
            raise ApprovalError(f"only drafts can be approved; this version is {v['status']}")
        blocks = [i["message"] for i in v["payload"].get("review_issues", []) if i["severity"] == "block"]
        if blocks and not acknowledge_blocks:
            raise ApprovalError(f"{len(blocks)} blocking issue(s) must be reviewed and acknowledged first: {blocks[0]}", 422)
        with self._lock:
            self._x("UPDATE versions SET status='superseded' WHERE project_id=? AND status='approved' AND id!=?", (v["project_id"], vid))
            self._x("UPDATE versions SET status='approved', approved_by=?, approved_at=datetime('now'), approved_hash=content_hash, approval_note=? WHERE id=?",
                    (reviewer, note, vid))
        self.log(reviewer, "version_approved", "version", vid, {"blocking_acknowledged": len(blocks), "note": note})
        return self.version(vid)

    def mark_sent(self, vid: int, sender: str, path: str) -> dict:
        self._x("UPDATE versions SET status='sent', sent_by=?, sent_at=datetime('now'), sent_path=? WHERE id=?", (sender, path, vid))
        self.log(sender, "version_sent", "version", vid, {"path": path})
        return self.version(vid)

    def assert_sendable(self, vid: int) -> dict:
        v = self.version(vid)
        if not v:
            raise ApprovalError("version not found", 404)
        if v["status"] == "sent":
            raise ApprovalError("this version was already sent")
        if v["status"] != "approved":
            raise ApprovalError("only an approved version can be sent to the client — approve it first")
        if v["approved_hash"] != content_hash(v["payload"]):
            raise ApprovalError("content changed after approval; it must be approved again")
        return v

    # jobs -------------------------------------------------------------------
    def create_job(self, project_id: int, meeting_id: str, stages: list[str]) -> int:
        st = [{"name": s, "status": "pending"} for s in stages]
        return self._x("INSERT INTO jobs(project_id, meeting_id, status, stages) VALUES(?,?,?,?)", (project_id, meeting_id, "running", json.dumps(st)))

    def job(self, jid: int) -> dict | None:
        rows = self._q("SELECT * FROM jobs WHERE id=?", (jid,))
        if not rows:
            return None
        j = rows[0]
        j["stages"] = json.loads(j["stages"])
        return j

    def job_stage(self, jid: int, stage: str, status: str, detail: str | None = None) -> None:
        with self._lock:
            j = self.job(jid)
            for s in j["stages"]:
                if s["name"] == stage:
                    s["status"] = status
                    if detail:
                        s["detail"] = detail
            self._x("UPDATE jobs SET stage=?, stages=? WHERE id=?", (stage, json.dumps(j["stages"]), jid))

    def finish_job(self, jid: int, status: str, version_id: int | None = None, error: str | None = None) -> None:
        self._x("UPDATE jobs SET status=?, version_id=?, error=?, finished_at=datetime('now') WHERE id=?", (status, version_id, error, jid))

    def jobs(self, project_id: int) -> list[dict]:
        rows = self._q("SELECT * FROM jobs WHERE project_id=? ORDER BY id DESC LIMIT 10", (project_id,))
        for r in rows:
            r["stages"] = json.loads(r["stages"])
        return rows
