import type { DB } from "../db.js";
import type { Category, ReviewDecision } from "../types.js";

/**
 * The documented "unchanged" rule used by the autonomy counter:
 * a draft counts as sent without changes when the final body equals the
 * drafted body after normalising line endings (CRLF -> LF), trailing spaces on
 * each line, runs of 3+ blank lines, and leading/trailing whitespace.
 * Any other difference — a single word, a comma — counts as an edit.
 */
export function normalizeForComparison(text: string): string {
  return text
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((l) => l.trimEnd())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export function isUnchanged(draft: string, final: string): boolean {
  return normalizeForComparison(draft) === normalizeForComparison(final);
}

/** Word-level edit distance divided by the longer length: 0 identical, 1 completely different. */
export function editRatio(a: string, b: string): number {
  const x = normalizeForComparison(a).split(/\s+/).filter(Boolean);
  const y = normalizeForComparison(b).split(/\s+/).filter(Boolean);
  if (!x.length && !y.length) return 0;
  let prev = Array.from({ length: y.length + 1 }, (_, j) => j);
  for (let i = 1; i <= x.length; i++) {
    const cur = [i];
    for (let j = 1; j <= y.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x[i - 1] === y[j - 1] ? 0 : 1));
    }
    prev = cur;
  }
  return Math.round((prev[y.length] / Math.max(x.length, y.length)) * 1000) / 1000;
}

export interface FeedbackRecord {
  draftId: number;
  emailId: string;
  category: Category;
  decision: ReviewDecision | "audit_ok" | "audit_flag";
  draftBody: string;
  finalBody: string | null;
  reason?: string | null;
  reviewer: string;
  promptVersion: string;
  model: string;
}

export class FeedbackStore {
  constructor(private db: DB) {}

  record(f: FeedbackRecord): { unchanged: boolean; editRatio: number } {
    // Never trust the button: "unchanged" is decided by comparing the text that was actually sent.
    const unchanged = (f.decision === "approve" || f.decision === "edit") && f.finalBody !== null && isUnchanged(f.draftBody, f.finalBody);
    const ratio = f.finalBody === null ? 1 : editRatio(f.draftBody, f.finalBody);
    this.db
      .prepare(
        `INSERT INTO feedback(draft_id, email_id, category, decision, unchanged, draft_body, final_body, edit_ratio, reason, reviewer, prompt_version, model)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(f.draftId, f.emailId, f.category, f.decision, unchanged ? 1 : 0, f.draftBody, f.finalBody, ratio, f.reason ?? null, f.reviewer, f.promptVersion, f.model);
    return { unchanged, editRatio: ratio };
  }

  /** Most recent human-approved final replies for a category: used as style examples in the drafting prompt. */
  exemplars(category: Category, limit = 2): { draft: string; final: string; edited: boolean }[] {
    const rows = this.db
      .prepare(
        `SELECT draft_body, final_body, unchanged FROM feedback
         WHERE category=? AND decision IN ('approve','edit') AND final_body IS NOT NULL
         ORDER BY unchanged ASC, id DESC LIMIT ?`,
      )
      .all(category, limit) as { draft_body: string; final_body: string; unchanged: number }[];
    return rows.map((r) => ({ draft: r.draft_body, final: r.final_body, edited: !r.unchanged }));
  }

  stats(): unknown[] {
    return this.db
      .prepare(
        `SELECT category, prompt_version, COUNT(*) AS reviewed,
                SUM(decision IN ('approve','edit') AND unchanged=1) AS sent_unchanged,
                SUM(decision='edit' AND unchanged=0) AS edited,
                SUM(decision='reject') AS rejected,
                SUM(decision='escalate') AS escalated,
                ROUND(AVG(edit_ratio), 3) AS avg_edit_ratio
         FROM feedback WHERE decision IN ('approve','edit','reject','escalate')
         GROUP BY category, prompt_version ORDER BY category, prompt_version`,
      )
      .all();
  }

  recent(limit = 50): unknown[] {
    return this.db.prepare("SELECT * FROM feedback ORDER BY id DESC LIMIT ?").all(limit);
  }

  editsForLearning(promptVersion: string, limit = 40): { category: string; draft_body: string; final_body: string; reason: string | null; edit_ratio: number }[] {
    return this.db
      .prepare(
        `SELECT category, draft_body, final_body, reason, edit_ratio FROM feedback
         WHERE prompt_version=? AND ((decision='edit' AND unchanged=0) OR (decision='reject' AND final_body IS NOT NULL))
         ORDER BY id DESC LIMIT ?`,
      )
      .all(promptVersion, limit) as never;
  }
}
