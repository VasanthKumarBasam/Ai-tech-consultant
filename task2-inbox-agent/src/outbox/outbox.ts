import fs from "node:fs";
import path from "node:path";
import type { DB } from "../db.js";

/**
 * SIMULATED email delivery (documented in README/DESIGN): a "sent" reply is
 * recorded in `sent_messages` and written as an .eml file under data/outbox/.
 * Swapping in SMTP/Gmail only means replacing `deliver()`.
 *
 * Idempotency: sent_messages.draft_id is UNIQUE, so a retried request or a
 * double click can never send the same draft twice.
 */
export interface SendRequest {
  draftId: number;
  emailId: string;
  to: string;
  subject: string;
  body: string;
  mode: "manual" | "autonomous";
  sentBy: string;
}

export interface SendResult {
  sentId: number;
  duplicate: boolean;
  sentAt: string;
}

export class Outbox {
  constructor(private db: DB, private dir: string, private fromAddress = "support@northstarweb.studio") {}

  send(req: SendRequest): SendResult {
    const existing = this.db.prepare("SELECT id, sent_at FROM sent_messages WHERE draft_id=?").get(req.draftId) as { id: number; sent_at: string } | undefined;
    if (existing) return { sentId: existing.id, duplicate: true, sentAt: existing.sent_at };
    const info = this.db
      .prepare("INSERT INTO sent_messages(draft_id, email_id, to_email, subject, body, mode, sent_by) VALUES (?, ?, ?, ?, ?, ?, ?)")
      .run(req.draftId, req.emailId, req.to, req.subject, req.body, req.mode, req.sentBy);
    const row = this.db.prepare("SELECT sent_at FROM sent_messages WHERE id=?").get(info.lastInsertRowid) as { sent_at: string };
    this.deliver(req, Number(info.lastInsertRowid));
    return { sentId: Number(info.lastInsertRowid), duplicate: false, sentAt: row.sent_at };
  }

  private deliver(req: SendRequest, id: number): void {
    try {
      fs.mkdirSync(this.dir, { recursive: true });
      const eml = [
        `From: ${this.fromAddress}`,
        `To: ${req.to}`,
        `Subject: ${req.subject}`,
        `X-Draft-Id: ${req.draftId}`,
        `X-Send-Mode: ${req.mode}`,
        "Content-Type: text/plain; charset=utf-8",
        "",
        req.body,
      ].join("\r\n");
      fs.writeFileSync(path.join(this.dir, `${String(id).padStart(5, "0")}-${req.emailId}.eml`), eml);
    } catch {
      /* the DB row is the source of truth; the .eml is a convenience copy */
    }
  }

  list(limit = 50): unknown[] {
    return this.db.prepare("SELECT * FROM sent_messages ORDER BY id DESC LIMIT ?").all(limit);
  }
}
