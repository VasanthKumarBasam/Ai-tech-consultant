import fs from "node:fs";
import path from "node:path";
import { TASK_DIR } from "../config.js";
import type { DB } from "../db.js";
import type { Category } from "../types.js";

export const PROMPT_DIR = process.env.INBOX_PROMPT_DIR ?? path.join(TASK_DIR, "prompts");

export type PromptName = "classify" | "draft";

/** Versions on disk: prompts/<name>.v<N>.md (+ draft.v<N>.guidelines.md for learned style rules). */
export function listVersionsOnDisk(name: PromptName): string[] {
  return fs
    .readdirSync(PROMPT_DIR)
    .map((f) => f.match(new RegExp(`^${name}\\.(v\\d+)\\.md$`))?.[1])
    .filter((v): v is string => !!v)
    .sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
}

export class PromptRegistry {
  constructor(private db: DB) {
    for (const name of ["classify", "draft"] as PromptName[]) {
      for (const v of listVersionsOnDisk(name)) {
        this.db
          .prepare("INSERT OR IGNORE INTO prompt_versions(name, version, status, notes) VALUES (?, ?, ?, ?)")
          .run(name, v, v === "v1" ? "active" : "candidate", v === "v1" ? "baseline" : "found on disk");
      }
    }
  }

  active(name: PromptName): string {
    const row = this.db.prepare("SELECT version FROM prompt_versions WHERE name=? AND status='active'").get(name) as { version: string } | undefined;
    return row?.version ?? "v1";
  }

  list(): { name: string; version: string; status: string; notes: string | null; created_at: string; activated_at: string | null }[] {
    return this.db.prepare("SELECT * FROM prompt_versions ORDER BY name, CAST(substr(version, 2) AS INTEGER)").all() as never;
  }

  /** Promote a version; the previously active one is retired (and can be re-activated = rollback). */
  activate(name: PromptName, version: string): void {
    if (!fs.existsSync(path.join(PROMPT_DIR, `${name}.${version}.md`))) throw new Error(`prompt ${name}.${version} not found`);
    const tx = this.db.transaction(() => {
      this.db.prepare("UPDATE prompt_versions SET status='retired' WHERE name=? AND status='active'").run(name);
      this.db
        .prepare(
          `INSERT INTO prompt_versions(name, version, status, activated_at) VALUES (?, ?, 'active', datetime('now'))
           ON CONFLICT(name, version) DO UPDATE SET status='active', activated_at=datetime('now')`,
        )
        .run(name, version);
    });
    tx();
  }

  register(name: PromptName, version: string, notes: string): void {
    this.db
      .prepare("INSERT OR REPLACE INTO prompt_versions(name, version, status, notes) VALUES (?, ?, 'candidate', ?)")
      .run(name, version, notes);
  }
}

export function loadTemplate(name: PromptName, version: string): string {
  return fs.readFileSync(path.join(PROMPT_DIR, `${name}.${version}.md`), "utf8");
}

export function loadGuidelines(version: string): string {
  const p = path.join(PROMPT_DIR, `draft.${version}.guidelines.md`);
  return fs.existsSync(p) ? fs.readFileSync(p, "utf8").trim() : "- (none)";
}

export const CATEGORY_POLICY: Record<Category, string> = {
  how_to: "Answer the question step by step from the knowledge sources and link the article. If the sources do not cover it, say a specialist will follow up.",
  troubleshooting: "Give the documented checks or fix from the sources, in order, and link the article. Ask for one specific detail (screenshot, URL, exact error) only if it would change the fix. Never guess at causes the sources don't mention.",
  project_request: "New work. Restate the goal, mention any existing platform capability from the sources that may cover part of it, and offer a short scoping call. No prices, estimates or dates.",
  refund_cancellation: "Money or cancellation. Acknowledge the request, confirm it has been passed to our billing team for review, and say they will reply. You may describe documented platform behaviour from the sources (e.g. how subscriptions work) but must not confirm, promise or deny any refund, cancellation, fee or amount.",
  complaint: "Acknowledge the specific issue and its impact, apologise for the experience (no admission of fault or liability), say an account manager will follow up personally. No compensation, credits or deadlines.",
  quote_request: "Pricing question. Thank them, restate the scope in one or two lines, note any existing capability from the sources, and propose a scoping call so we can send a written estimate. Never give a number, range or timeline.",
  follow_up: "Reference their earlier message using the thread history, give the current status only if the history shows it, otherwise say you are checking and who will reply. Do not claim work was done unless the history shows it.",
  other: "Reply briefly and politely; route to the right person if needed. No commitments.",
};
