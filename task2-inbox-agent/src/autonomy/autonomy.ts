import type { Config } from "../config.js";
import type { DB } from "../db.js";
import { CATEGORIES, NEVER_AUTONOMOUS, type Category, type Classification, type GuardIssue, type Risk } from "../types.js";

/**
 * The 20-consecutive rule, implemented conservatively.
 *
 * "Same kind"  = the same email category (one counter per category).
 * "Unchanged"  = final sent text equals the draft under normalizeForComparison()
 *                (see feedback.ts) — the approve button alone never counts.
 *
 * Counter: +1 for every draft in the category sent unchanged by a human;
 *          reset to 0 on an edit, rejection, expert escalation or a post-send audit flag.
 * Eligible: counter >= threshold (20) and category is not in NEVER_AUTONOMOUS.
 * Enabled:  eligibility only makes a category *eligible*; a named person must switch it on.
 * Per-draft gate (canAutoSend): enabled AND low risk AND no warn/block guard AND the knowledge
 *          lookup fully answered AND no legal/security/chargeback/injection flag.
 * Kill switches: any edit/reject/flag while enabled disables it; so does an edit rate above
 *          maxEditRate over the last `window` human reviews; every Nth autonomous send is queued
 *          for a post-send audit by a person.
 */

export interface AutonomyState {
  category: Category;
  consecutive_unedited: number;
  best_streak: number;
  eligible: number;
  enabled: number;
  auto_sent: number;
  disabled_reason: string | null;
  updated_at: string;
  never_autonomous: boolean;
  threshold: number;
}

export type ReviewOutcome = "unchanged" | "edited" | "rejected" | "escalated" | "audit_flag" | "audit_ok";

const BLOCKING_FLAGS = new Set(["legal_threat", "chargeback_or_dispute", "security_or_account_access", "possible_prompt_injection", "angry_customer"]);

export class Autonomy {
  constructor(private db: DB, private cfg: Config) {
    const ins = db.prepare("INSERT OR IGNORE INTO autonomy_state(category) VALUES (?)");
    for (const c of CATEGORIES) ins.run(c);
  }

  private event(category: Category, event: string, draftId: number | null, streak: number | null, detail?: string, actor?: string) {
    this.db
      .prepare("INSERT INTO autonomy_events(category, event, draft_id, streak_after, detail, actor) VALUES (?, ?, ?, ?, ?, ?)")
      .run(category, event, draftId, streak, detail ?? null, actor ?? null);
  }

  get(category: Category): AutonomyState {
    const row = this.db.prepare("SELECT * FROM autonomy_state WHERE category=?").get(category) as Omit<AutonomyState, "never_autonomous" | "threshold">;
    return { ...row, never_autonomous: NEVER_AUTONOMOUS.has(category), threshold: this.cfg.autonomy.threshold };
  }

  all(): AutonomyState[] {
    return CATEGORIES.map((c) => this.get(c));
  }

  events(limit = 100): unknown[] {
    return this.db.prepare("SELECT * FROM autonomy_events ORDER BY id DESC LIMIT ?").all(limit);
  }

  /** Called for every human review outcome. Returns the updated state. */
  recordReview(category: Category, outcome: ReviewOutcome, draftId: number, actor: string): AutonomyState {
    const tx = this.db.transaction(() => {
      const s = this.get(category);
      if (outcome === "audit_ok") {
        this.event(category, "audit_ok", draftId, s.consecutive_unedited, undefined, actor);
        return;
      }
      if (outcome === "unchanged") {
        const streak = s.consecutive_unedited + 1;
        const eligible = streak >= this.cfg.autonomy.threshold && !NEVER_AUTONOMOUS.has(category);
        this.db
          .prepare("UPDATE autonomy_state SET consecutive_unedited=?, best_streak=MAX(best_streak, ?), eligible=?, updated_at=datetime('now') WHERE category=?")
          .run(streak, streak, eligible ? 1 : 0, category);
        this.event(category, "sent_unchanged", draftId, streak, undefined, actor);
        if (eligible && !s.eligible) this.event(category, "became_eligible", draftId, streak, `${streak} consecutive unchanged drafts`);
      } else {
        this.db
          .prepare("UPDATE autonomy_state SET consecutive_unedited=0, eligible=0, updated_at=datetime('now') WHERE category=?")
          .run(category);
        this.event(category, `streak_reset_${outcome}`, draftId, 0, undefined, actor);
        if (s.enabled) this.disable(category, `auto-disabled: a draft was ${outcome.replace("_", " ")}`, "system", draftId);
      }
      this.checkRollingEditRate(category);
    });
    tx();
    return this.get(category);
  }

  private checkRollingEditRate(category: Category) {
    const s = this.get(category);
    if (!s.enabled) return;
    const rows = this.db
      .prepare("SELECT unchanged FROM feedback WHERE category=? AND decision IN ('approve','edit','reject','escalate') ORDER BY id DESC LIMIT ?")
      .all(category, this.cfg.autonomy.window) as { unchanged: number }[];
    if (rows.length < this.cfg.autonomy.window) return;
    const rate = rows.filter((r) => !r.unchanged).length / rows.length;
    if (rate > this.cfg.autonomy.maxEditRate) this.disable(category, `auto-disabled: edit rate ${(rate * 100).toFixed(0)}% over last ${rows.length} reviews`, "system", null);
  }

  enable(category: Category, actor: string): AutonomyState {
    const s = this.get(category);
    if (NEVER_AUTONOMOUS.has(category)) throw new Error(`${category} always requires a human reviewer`);
    if (!s.eligible) throw new Error(`${category} is not eligible yet (${s.consecutive_unedited}/${this.cfg.autonomy.threshold} consecutive unchanged drafts)`);
    this.db.prepare("UPDATE autonomy_state SET enabled=1, disabled_reason=NULL, updated_at=datetime('now') WHERE category=?").run(category);
    this.event(category, "enabled", null, s.consecutive_unedited, undefined, actor);
    return this.get(category);
  }

  disable(category: Category, reason: string, actor: string, draftId: number | null = null): AutonomyState {
    this.db.prepare("UPDATE autonomy_state SET enabled=0, disabled_reason=?, updated_at=datetime('now') WHERE category=?").run(reason, category);
    this.event(category, "disabled", draftId, null, reason, actor);
    return this.get(category);
  }

  /** Per-draft gate. Returns ok=false with every reason a human is needed. */
  canAutoSend(input: { category: Category; risk: Risk; classification: Classification; guards: GuardIssue[]; kbStatus: string; holdingReply: boolean }): { ok: boolean; reasons: string[] } {
    const reasons: string[] = [];
    const s = this.get(input.category);
    if (NEVER_AUTONOMOUS.has(input.category)) reasons.push(`${input.category.replace("_", " ")} emails always need a person`);
    else if (!s.eligible) reasons.push(`autonomy not earned yet (${s.consecutive_unedited}/${this.cfg.autonomy.threshold} unchanged in a row)`);
    else if (!s.enabled) reasons.push("eligible for autonomy, but nobody has switched it on");
    if (input.risk !== "low") reasons.push(`risk is ${input.risk}`);
    for (const f of input.classification.flags) if (BLOCKING_FLAGS.has(f)) reasons.push(`flag: ${f.replace(/_/g, " ")}`);
    for (const g of input.guards) if (g.severity !== "info") reasons.push(`check: ${g.code.replace(/_/g, " ")}`);
    if (input.holdingReply) reasons.push("knowledge service unavailable");
    else if (!["answered", "not_needed"].includes(input.kbStatus)) reasons.push(`knowledge lookup ${input.kbStatus.replace(/_/g, " ")}`);
    return { ok: reasons.length === 0, reasons };
  }

  /** Records an autonomous send; every Nth one goes to the post-send audit queue. */
  recordAutoSend(category: Category, draftId: number): { audit: boolean } {
    this.db.prepare("UPDATE autonomy_state SET auto_sent=auto_sent+1 WHERE category=?").run(category);
    const n = this.get(category).auto_sent;
    this.event(category, "auto_sent", draftId, null, `autonomous send #${n}`);
    return { audit: n % this.cfg.autonomy.auditEvery === 1 || this.cfg.autonomy.auditEvery === 1 };
  }
}
