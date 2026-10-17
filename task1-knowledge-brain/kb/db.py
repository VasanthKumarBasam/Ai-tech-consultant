"""SQLite storage: manifest, article versions, chunks (+FTS5), images, runs, escalations.

SQLite keeps the project a one-command install (no database server) while still
giving us transactions, full-text search (FTS5) and a durable crawl manifest.
Vectors are stored as float32 blobs and searched in memory with numpy, which is
plenty for a help centre of a few thousand chunks (see DESIGN.md for the
pgvector upgrade path).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Crawl manifest: one row per URL ever seen in the sitemap.
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    article_id TEXT,
    lastmod TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | indexed | failed | removed | skipped_robots
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    http_status INTEGER,
    etag TEXT,
    last_modified_header TEXT,
    content_hash TEXT,
    active_version_id INTEGER,
    active INTEGER NOT NULL DEFAULT 0,
    first_seen_run INTEGER,
    last_seen_run INTEGER,
    removed_in_run INTEGER,
    last_fetched_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status);

-- Every distinct parsed version of an article (enables rollback).
CREATE TABLE IF NOT EXISTS article_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    article_id TEXT,
    title TEXT NOT NULL,
    body_text TEXT NOT NULL,
    sections_json TEXT NOT NULL,
    images_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    lastmod TEXT,
    modified_on_text TEXT,
    run_id INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_versions_url ON article_versions(url);

-- Retrieval units. kind: text | image | expert
CREATE TABLE IF NOT EXISTS chunks (
    rowid_ INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id TEXT UNIQUE NOT NULL,
    version_id INTEGER,
    expert_id INTEGER,
    url TEXT NOT NULL,
    article_id TEXT,
    title TEXT NOT NULL,
    section TEXT,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    image_url TEXT,
    image_refs_json TEXT,
    embedding BLOB,
    dim INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chunks_active ON chunks(is_active);
CREATE INDEX IF NOT EXISTS idx_chunks_version ON chunks(version_id);

-- Keyword index over ACTIVE chunks only (rows are removed when a chunk is deactivated).
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED, title, section, text, tokenize='porter unicode61'
);

-- Vision descriptions are cached by image URL so re-ingestion never re-pays.
CREATE TABLE IF NOT EXISTS image_descriptions (
    image_key TEXT PRIMARY KEY,
    image_url TEXT NOT NULL,
    sha256 TEXT,
    description TEXT NOT NULL,
    ui_labels_json TEXT,
    legible INTEGER,
    uncertainty TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,          -- running | completed | failed | interrupted | rolled_back
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    heartbeat_at TEXT,
    finished_at TEXT,
    counts_json TEXT,
    error TEXT,
    params_json TEXT
);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    normalized_question TEXT NOT NULL,
    context TEXT,
    reason TEXT NOT NULL,
    evidence_json TEXT,
    requester TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- open | resolved | dismissed
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_by TEXT,
    answer TEXT,
    scope TEXT,                    -- general | customer_specific
    expires_at TEXT,
    expert_id INTEGER
);

-- Human-approved answers. Versioned; only 'general' scope ones are indexed.
CREATE TABLE IF NOT EXISTS expert_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    escalation_id INTEGER,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    supporting_url TEXT,
    approved_by TEXT NOT NULL,
    scope TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS query_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    sources_json TEXT,
    model TEXT,
    prompt_version TEXT,
    retrieval_mode TEXT,
    top_similarity REAL,
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    escalation_id INTEGER
);
"""

_lock = threading.RLock()


class Database:
    """Thin wrapper: one connection per thread, serialized writes."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        with _lock:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # convenience helpers -------------------------------------------------
    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(sql, params))

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.connect() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid

    def get_meta(self, key: str) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
