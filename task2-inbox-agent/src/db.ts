import fs from "node:fs";
import path from "node:path";
import Database from "better-sqlite3";

export type DB = Database.Database;

const SCHEMA = `
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS emails (
  id TEXT PRIMARY KEY,
  thread_id TEXT,
  from_name TEXT NOT NULL,
  from_email TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  received_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',      -- new | processing | pending_review | sent | rejected | escalated | error
  category TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_emails_thread ON emails(thread_id);

CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id TEXT NOT NULL REFERENCES emails(id),
  attempt INTEGER NOT NULL DEFAULT 1,
  category TEXT NOT NULL,
  risk TEXT NOT NULL,
  classification_json TEXT NOT NULL,
  knowledge_json TEXT NOT NULL,
  kb_status TEXT NOT NULL,                 -- answered | partial | insufficient_evidence | unavailable | not_needed | error
  draft_json TEXT NOT NULL,
  reply_subject TEXT NOT NULL,
  reply_body TEXT NOT NULL,
  guards_json TEXT NOT NULL,
  approval_reasons_json TEXT NOT NULL,
  requires_approval INTEGER NOT NULL,
  holding_reply INTEGER NOT NULL DEFAULT 0, -- 1 = no-facts acknowledgement because knowledge was unavailable
  status TEXT NOT NULL DEFAULT 'pending_review', -- pending_review | sent | auto_sent | rejected | escalated | superseded
  audit_status TEXT,                       -- autonomous sends only: pending | ok | flagged
  models_json TEXT NOT NULL,
  prompt_versions_json TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  reviewed_at TEXT,
  reviewer TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_email ON drafts(email_id);

-- Every human decision, with the exact draft and final text (the learning signal).
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  draft_id INTEGER NOT NULL REFERENCES drafts(id),
  email_id TEXT NOT NULL,
  category TEXT NOT NULL,
  decision TEXT NOT NULL,                  -- approve | edit | reject | escalate | audit_ok | audit_flag
  unchanged INTEGER NOT NULL,              -- 1 if final text == draft text under the documented normalisation
  draft_body TEXT NOT NULL,
  final_body TEXT,
  edit_ratio REAL NOT NULL DEFAULT 0,      -- 0 = identical, 1 = completely different (word-level)
  reason TEXT,
  reviewer TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  model TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_cat ON feedback(category, id);

CREATE TABLE IF NOT EXISTS sent_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  draft_id INTEGER NOT NULL UNIQUE REFERENCES drafts(id),  -- idempotency: one send per draft
  email_id TEXT NOT NULL,
  to_email TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  mode TEXT NOT NULL,                      -- manual | autonomous
  sent_by TEXT NOT NULL,
  sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS autonomy_state (
  category TEXT PRIMARY KEY,
  consecutive_unedited INTEGER NOT NULL DEFAULT 0,
  best_streak INTEGER NOT NULL DEFAULT 0,
  eligible INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 0,
  auto_sent INTEGER NOT NULL DEFAULT 0,
  disabled_reason TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS autonomy_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL,
  event TEXT NOT NULL,
  draft_id INTEGER,
  streak_after INTEGER,
  detail TEXT,
  actor TEXT,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prompt_versions (
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  status TEXT NOT NULL,                    -- active | candidate | retired
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  activated_at TEXT,
  PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_id TEXT,
  detail TEXT
);
`;

export function openDb(dbPath: string): DB {
  if (dbPath !== ":memory:") fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  const db = new Database(dbPath);
  db.exec(SCHEMA);
  return db;
}
