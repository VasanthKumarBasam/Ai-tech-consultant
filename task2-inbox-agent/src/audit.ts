import type { DB } from "./db.js";

/** Masks e-mail addresses and phone numbers before anything is written to the audit log. */
export function maskPII(text: string): string {
  return text
    .replace(/([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})/g, "$1***@$2")
    .replace(/\+?\d[\d\s().-]{7,}\d/g, (m) => (m.replace(/\D/g, "").length >= 9 ? "[phone]" : m));
}

export class Audit {
  constructor(private db: DB) {}

  log(actor: string, action: string, entity: string, entityId: string | number | null, detail?: unknown): void {
    const d = detail === undefined ? null : maskPII(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.db
      .prepare("INSERT INTO audit_log(actor, action, entity, entity_id, detail) VALUES (?, ?, ?, ?, ?)")
      .run(actor, action, entity, entityId === null ? null : String(entityId), d);
  }

  recent(limit = 100): unknown[] {
    return this.db.prepare("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?").all(limit);
  }
}
