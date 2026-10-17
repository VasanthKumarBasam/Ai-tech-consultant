import { randomUUID } from "node:crypto";
import type { Audit } from "./audit.js";
import type { Autonomy } from "./autonomy/autonomy.js";
import type { Config } from "./config.js";
import type { DB } from "./db.js";
import { CATEGORY_POLICY, loadGuidelines, loadTemplate, type PromptRegistry } from "./drafting/prompts.js";
import { detectFlags, minRisk } from "./email/signals.js";
import { FeedbackStore, isUnchanged } from "./feedback/feedback.js";
import { KnowledgeClient, KnowledgeRequestError, KnowledgeUnavailableError } from "./knowledge-client/client.js";
import { LLMError, type LLM, type Usage } from "./llm/claude.js";
import type { Outbox } from "./outbox/outbox.js";
import {
  ClassificationSchema,
  DraftSchema,
  type Category,
  type Classification,
  type Draft,
  type GuardIssue,
  type InboundEmail,
  type KnowledgeLookup,
  type KnowledgeSource,
} from "./types.js";
import { checkDraft, hasSeverity } from "./validation/guards.js";

export interface Deps {
  cfg: Config;
  db: DB;
  llm: LLM;
  kb: KnowledgeClient;
  prompts: PromptRegistry;
  feedback: FeedbackStore;
  autonomy: Autonomy;
  outbox: Outbox;
  audit: Audit;
}

export interface DraftRow {
  id: number;
  email_id: string;
  category: Category;
  risk: string;
  status: string;
  reply_subject: string;
  reply_body: string;
  kb_status: string;
  holding_reply: number;
  requires_approval: number;
  audit_status: string | null;
  classification: Classification;
  knowledge: KnowledgeLookup[];
  draft: Draft;
  guards: GuardIssue[];
  approval_reasons: string[];
  models: { classify: string; draft: string };
  prompt_versions: { classify: string; draft: string };
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  latency_ms: number;
  created_at: string;
  reviewed_at: string | null;
  reviewer: string | null;
}

export class ProcessingError extends Error {}
export class ReviewError extends Error {
  constructor(message: string, public readonly status = 409) {
    super(message);
  }
}

const KNOWLEDGE_CATEGORIES: ReadonlySet<Category> = new Set(["how_to", "troubleshooting"]);

function addUsage(total: Usage, u: Usage) {
  total.inputTokens += u.inputTokens;
  total.outputTokens += u.outputTokens;
  total.costUsd += u.costUsd;
  total.latencyMs += u.latencyMs;
}

function emailBlock(e: InboundEmail): string {
  return `From: ${e.from_name} <${e.from_email}>\nSubject: ${e.subject}\nReceived: ${e.received_at}\n\n${e.body}`;
}

export function aggregateKbStatus(lookups: KnowledgeLookup[]): string {
  if (!lookups.length) return "not_needed";
  if (lookups.some((l) => l.status === "unavailable")) return "unavailable";
  if (lookups.some((l) => l.status === "error")) return "error";
  if (lookups.every((l) => l.status === "answered")) return "answered";
  if (lookups.some((l) => l.status === "answered" || l.status === "partial")) return "partial";
  return "insufficient_evidence";
}

export class InboxPipeline {
  constructor(private d: Deps) {}

  // ------------------------------------------------------------------ emails
  insertEmail(e: Omit<InboundEmail, "received_at"> & { received_at?: string }): InboundEmail {
    const email: InboundEmail = { ...e, received_at: e.received_at ?? new Date().toISOString() };
    this.d.db
      .prepare("INSERT OR IGNORE INTO emails(id, thread_id, from_name, from_email, subject, body, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)")
      .run(email.id, email.thread_id, email.from_name, email.from_email, email.subject, email.body, email.received_at);
    this.d.audit.log("system", "email_received", "email", email.id, { from: email.from_email, subject: email.subject });
    return email;
  }

  getEmail(id: string): (InboundEmail & { status: string; category: string | null; last_error: string | null }) | undefined {
    return this.d.db.prepare("SELECT * FROM emails WHERE id=?").get(id) as never;
  }

  threadHistory(email: InboundEmail): string {
    if (!email.thread_id) return "(no earlier messages in this thread)";
    const rows = this.d.db
      .prepare(
        `SELECT e.id, e.received_at, e.from_name, e.subject, e.body, s.body AS reply, s.sent_at
         FROM emails e LEFT JOIN sent_messages s ON s.email_id = e.id
         WHERE e.thread_id = ? AND e.id != ? AND e.received_at <= ? ORDER BY e.received_at`,
      )
      .all(email.thread_id, email.id, email.received_at) as { received_at: string; from_name: string; subject: string; body: string; reply: string | null; sent_at: string | null }[];
    if (!rows.length) return "(no earlier messages in this thread)";
    return rows
      .map((r) => `[${r.received_at}] Customer (${r.from_name}) wrote — ${r.subject}:\n${r.body.slice(0, 1200)}\n` + (r.reply ? `[${r.sent_at}] We replied:\n${r.reply.slice(0, 1200)}` : "We have not replied to this message yet."))
      .join("\n\n");
  }

  // ---------------------------------------------------------------- process
  async classify(email: InboundEmail, model?: string): Promise<{ classification: Classification; usage: Usage; version: string }> {
    const version = this.d.prompts.active("classify");
    const system = loadTemplate("classify", version).replaceAll("{{company}}", this.d.cfg.companyName);
    const user = `<customer_email>\n${emailBlock(email)}\n</customer_email>\n<thread_history>\n${this.threadHistory(email)}\n</thread_history>\nClassify this email.`;
    const res = await this.d.llm.structured({
      model: model ?? this.d.cfg.classifyModel,
      system,
      user,
      toolName: "classify_email",
      toolDescription: "Record the triage classification of the email.",
      schema: ClassificationSchema,
      maxTokens: 600,
    });
    const c = res.data;
    const flags = [...new Set([...c.flags, ...detectFlags(`${email.subject}\n${email.body}`)])] as Classification["flags"];
    return { classification: { ...c, flags, risk: minRisk(c.category, flags, c.risk) }, usage: res.usage, version };
  }

  async lookup(email: InboundEmail, c: Classification, requestId: string, kbModel?: string): Promise<KnowledgeLookup[]> {
    let questions = c.product_questions.slice(0, 3);
    if (!questions.length && KNOWLEDGE_CATEGORIES.has(c.category)) questions = [c.summary || email.subject];
    if (!questions.length) return [];
    const context = `Customer email subject: ${email.subject}\n${email.body.slice(0, 1500)}`;
    return Promise.all(
      questions.map(async (q, i): Promise<KnowledgeLookup> => {
        try {
          const r = await this.d.kb.query(q, { context, requester: "inbox-agent", requestId: `${requestId}-${i}`, model: kbModel });
          const cited = r.sources.filter((s) => s.cited);
          return {
            question: q,
            status: r.status,
            answer: r.answer,
            escalation_id: r.escalation_id,
            request_id: r.request_id,
            latency_ms: r.latency_ms,
            cost_usd: Number(r.usage?.cost_usd ?? 0),
            error: r.error ?? undefined,
            sources: (cited.length ? cited : []).map((s) => ({ id: s.id, title: s.title, url: s.url, kind: s.kind, section: s.section, snippet: s.supporting_quote || s.snippet, image_url: s.image_url ?? null })),
          };
        } catch (e) {
          const unavailable = e instanceof KnowledgeUnavailableError;
          return { question: q, status: unavailable ? "unavailable" : "error", answer: "", sources: [], escalation_id: null, error: (e as Error).message };
        }
      }),
    );
  }

  /** Re-number sources across lookups as K1..Kn (deduplicated by URL + section). */
  static numberSources(lookups: KnowledgeLookup[]): KnowledgeSource[] {
    const out: KnowledgeSource[] = [];
    const seen = new Map<string, string>();
    for (const l of lookups) {
      l.sources = l.sources.map((s) => {
        const key = `${s.url}#${s.section}#${s.kind}`;
        if (!seen.has(key)) {
          const id = `K${out.length + 1}`;
          seen.set(key, id);
          out.push({ ...s, id });
        }
        return { ...s, id: seen.get(key)! };
      });
    }
    return out;
  }

  static knowledgeBlock(lookups: KnowledgeLookup[]): string {
    if (!lookups.length) return "(no product lookup was needed for this email)";
    return lookups
      .map((l) => {
        const srcs = l.sources
          .map((s) => `  <source id="${s.id}" title="${s.title.replace(/"/g, "'")}" url="${s.url}" kind="${s.kind}">${s.snippet.replace(/\s+/g, " ").slice(0, 500)}</source>`)
          .join("\n");
        const answer = l.status === "unavailable" ? "(knowledge service unavailable)" : l.answer.replace(/\[S(\d+)\]/g, "");
        return `<lookup question="${l.question.replace(/"/g, "'")}">\n  <answer status="${l.status}">${answer}</answer>\n${srcs}\n</lookup>`;
      })
      .join("\n");
  }

  holdingReply(email: InboundEmail, c: Classification): Draft {
    const name = c.customer_first_name ? ` ${c.customer_first_name}` : "";
    return {
      request_summary: c.summary,
      reply_subject: `Re: ${email.subject}`,
      reply_body: `Hi${name},\n\nThanks for getting in touch. We've received your message about "${email.subject}" and someone from our team is looking into it now. We'll reply with the details once we've checked.\n\nBest regards,\n${this.d.cfg.signature}`,
      claims: [],
      commitments: ["we will reply with details"],
      next_action: "Knowledge service was unavailable: retry drafting once it is back.",
      follow_up_date: null,
    };
  }

  async draft(email: InboundEmail, c: Classification, lookups: KnowledgeLookup[], sources: KnowledgeSource[], model?: string, versionOverride?: string): Promise<{ draft: Draft; usage: Usage; version: string }> {
    const version = versionOverride ?? this.d.prompts.active("draft");
    const system = loadTemplate("draft", version)
      .replaceAll("{{company}}", this.d.cfg.companyName)
      .replaceAll("{{signature}}", this.d.cfg.signature)
      .replaceAll("{{category_policy}}", CATEGORY_POLICY[c.category])
      .replaceAll("{{style_guidelines}}", loadGuidelines(version));
    const examples = this.d.feedback.exemplars(c.category, 2);
    const exampleBlock = examples.length
      ? examples.map((x, i) => `<example n="${i + 1}" edited_by_reviewer="${x.edited}">\n${x.final.slice(0, 1500)}\n</example>`).join("\n")
      : "(none yet)";
    const user = [
      `<customer_email>\n${emailBlock(email)}\n</customer_email>`,
      `<classification>\n${JSON.stringify({ category: c.category, risk: c.risk, sentiment: c.sentiment, summary: c.summary, flags: c.flags, customer_first_name: c.customer_first_name })}\n</classification>`,
      `<thread_history>\n${this.threadHistory(email)}\n</thread_history>`,
      `<knowledge>\n${InboxPipeline.knowledgeBlock(lookups)}\n</knowledge>`,
      `<approved_examples>\nRecent replies our reviewers sent in this category (match their style, not their facts):\n${exampleBlock}\n</approved_examples>`,
      "Draft the reply now.",
    ].join("\n");
    const ids = new Set(sources.map((s) => s.id));
    const res = await this.d.llm.structured({
      model: model ?? this.d.cfg.draftModel,
      system,
      user,
      toolName: "draft_reply",
      toolDescription: "Submit the reply draft with its claims and commitments.",
      schema: DraftSchema,
      maxTokens: 1800,
      validate: (d) => {
        const bad = d.claims.flatMap((cl) => cl.source_ids).filter((s) => !ids.has(s));
        return bad.length ? `claims cite unknown source ids ${[...new Set(bad)].join(", ")}; valid ids: ${[...ids].join(", ") || "(none — state no product facts)"}` : null;
      },
    });
    return { draft: res.data, usage: res.usage, version };
  }

  async process(emailId: string, opts: { actor?: string } = {}): Promise<DraftRow> {
    const email = this.getEmail(emailId);
    if (!email) throw new ProcessingError(`email ${emailId} not found`);
    if (email.status === "sent") throw new ReviewError("this email has already been answered");
    const t0 = Date.now();
    const requestId = randomUUID().slice(0, 8);
    this.d.db.prepare("UPDATE emails SET status='processing', last_error=NULL WHERE id=?").run(emailId);
    const usage: Usage = { model: "", inputTokens: 0, outputTokens: 0, latencyMs: 0, costUsd: 0, retries: 0 };
    try {
      const cls = await this.classify(email);
      addUsage(usage, cls.usage);
      const c = cls.classification;
      const lookups = await this.lookup(email, c, requestId);
      const sources = InboxPipeline.numberSources(lookups);
      const kbStatus = aggregateKbStatus(lookups);
      const holding = kbStatus === "unavailable";
      let draft: Draft;
      let draftVersion = this.d.prompts.active("draft");
      if (holding) {
        draft = this.holdingReply(email, c);
      } else {
        const dr = await this.draft(email, c, lookups, sources);
        addUsage(usage, dr.usage);
        draft = dr.draft;
        draftVersion = dr.version;
      }
      const guards = checkDraft({ draft, category: c.category, classification: c, sources, kbStatus, holdingReply: holding, cfg: this.d.cfg });
      const gate = this.d.autonomy.canAutoSend({ category: c.category, risk: c.risk, classification: c, guards, kbStatus, holdingReply: holding });
      const kbCost = lookups.reduce((a, l) => a + (l.cost_usd ?? 0), 0);
      const draftId = this.d.db.transaction(() => {
        this.d.db.prepare("UPDATE drafts SET status='superseded' WHERE email_id=? AND status='pending_review'").run(emailId);
        const attempt = (this.d.db.prepare("SELECT COUNT(*) n FROM drafts WHERE email_id=?").get(emailId) as { n: number }).n + 1;
        const info = this.d.db
          .prepare(
            `INSERT INTO drafts(email_id, attempt, category, risk, classification_json, knowledge_json, kb_status, draft_json, reply_subject, reply_body,
               guards_json, approval_reasons_json, requires_approval, holding_reply, models_json, prompt_versions_json, input_tokens, output_tokens, cost_usd, latency_ms)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          )
          .run(
            emailId, attempt, c.category, c.risk, JSON.stringify(c), JSON.stringify(lookups), kbStatus, JSON.stringify(draft), draft.reply_subject, draft.reply_body,
            JSON.stringify(guards), JSON.stringify(gate.reasons), gate.ok ? 0 : 1, holding ? 1 : 0,
            JSON.stringify({ classify: this.modelLabel(this.d.cfg.classifyModel), draft: holding ? "template" : this.modelLabel(this.d.cfg.draftModel) }),
            JSON.stringify({ classify: cls.version, draft: holding ? "holding-template" : draftVersion }),
            usage.inputTokens, usage.outputTokens, usage.costUsd + kbCost, Date.now() - t0,
          );
        this.d.db.prepare("UPDATE emails SET status='pending_review', category=? WHERE id=?").run(c.category, emailId);
        return Number(info.lastInsertRowid);
      })();
      this.d.audit.log(opts.actor ?? "system", "draft_created", "draft", draftId, { email: emailId, category: c.category, risk: c.risk, kb_status: kbStatus, guards: guards.map((g) => g.code), auto: gate.ok });
      if (gate.ok) this.autoSend(draftId);
      return this.getDraft(draftId)!;
    } catch (e) {
      const msg = e instanceof LLMError || e instanceof KnowledgeRequestError ? e.message : `${(e as Error).name}: ${(e as Error).message}`;
      this.d.db.prepare("UPDATE emails SET status='error', last_error=? WHERE id=?").run(msg.slice(0, 500), emailId);
      this.d.audit.log("system", "processing_failed", "email", emailId, msg);
      throw new ProcessingError(msg);
    }
  }

  private modelLabel(model: string): string {
    return this.d.llm.provider === "anthropic" ? model : this.d.llm.provider;
  }

  private autoSend(draftId: number) {
    const dr = this.getDraft(draftId)!;
    const email = this.getEmail(dr.email_id)!;
    const res = this.d.outbox.send({ draftId, emailId: email.id, to: email.from_email, subject: dr.reply_subject, body: dr.reply_body, mode: "autonomous", sentBy: "autonomy" });
    const { audit } = this.d.autonomy.recordAutoSend(dr.category, draftId);
    this.d.db.prepare("UPDATE drafts SET status='auto_sent', audit_status=? WHERE id=?").run(audit ? "pending" : null, draftId);
    this.d.db.prepare("UPDATE emails SET status='sent' WHERE id=?").run(email.id);
    this.d.audit.log("autonomy", "auto_sent", "draft", draftId, { category: dr.category, sent_id: res.sentId, audit_queued: audit });
  }

  // ------------------------------------------------------------------ review
  getDraft(id: number): DraftRow | undefined {
    const r = this.d.db.prepare("SELECT * FROM drafts WHERE id=?").get(id) as Record<string, unknown> | undefined;
    return r ? InboxPipeline.hydrate(r) : undefined;
  }

  latestDraft(emailId: string): DraftRow | undefined {
    const r = this.d.db.prepare("SELECT * FROM drafts WHERE email_id=? ORDER BY id DESC LIMIT 1").get(emailId) as Record<string, unknown> | undefined;
    return r ? InboxPipeline.hydrate(r) : undefined;
  }

  static hydrate(r: Record<string, unknown>): DraftRow {
    const j = (k: string) => JSON.parse(String(r[k]));
    return {
      ...(r as unknown as DraftRow),
      classification: j("classification_json"),
      knowledge: j("knowledge_json"),
      draft: j("draft_json"),
      guards: j("guards_json"),
      approval_reasons: j("approval_reasons_json"),
      models: j("models_json"),
      prompt_versions: j("prompt_versions_json"),
    };
  }

  private pendingDraft(id: number): DraftRow {
    const d = this.getDraft(id);
    if (!d) throw new ReviewError("draft not found", 404);
    if (d.status !== "pending_review") throw new ReviewError(`draft is ${d.status.replace("_", " ")}, not awaiting review`);
    return d;
  }

  private finishReview(d: DraftRow, decision: "approve" | "edit" | "reject" | "escalate", reviewer: string, finalBody: string | null, finalSubject: string | null, reason: string | null) {
    const email = this.getEmail(d.email_id)!;
    const fb = this.d.feedback.record({
      draftId: d.id, emailId: d.email_id, category: d.category, decision, draftBody: d.reply_body, finalBody,
      reason, reviewer, promptVersion: d.prompt_versions.draft, model: d.models.draft,
    });
    const outcome = decision === "escalate" ? "escalated" : decision === "reject" ? "rejected" : fb.unchanged ? "unchanged" : "edited";
    let sent: { sentId: number; duplicate: boolean } | null = null;
    if (finalBody !== null && decision !== "escalate") {
      sent = this.d.outbox.send({ draftId: d.id, emailId: email.id, to: email.from_email, subject: finalSubject ?? d.reply_subject, body: finalBody, mode: "manual", sentBy: reviewer });
    }
    const draftStatus = decision === "escalate" ? "escalated" : decision === "reject" ? "rejected" : "sent";
    this.d.db.prepare("UPDATE drafts SET status=?, reviewed_at=datetime('now'), reviewer=? WHERE id=?").run(draftStatus, reviewer, d.id);
    this.d.db.prepare("UPDATE emails SET status=? WHERE id=?").run(sent ? "sent" : decision === "escalate" ? "escalated" : "rejected", email.id);
    // holding replies say nothing about the product: they don't count toward (or against) autonomy
    const autonomy = d.holding_reply ? this.d.autonomy.get(d.category) : this.d.autonomy.recordReview(d.category, outcome, d.id, reviewer);
    this.d.audit.log(reviewer, `review_${decision}`, "draft", d.id, { outcome, edit_ratio: fb.editRatio, reason, sent: sent?.sentId ?? null });
    return { draft: this.getDraft(d.id)!, outcome, edit_ratio: fb.editRatio, sent, autonomy };
  }

  private assertNoUnacknowledgedBlocks(issues: GuardIssue[], acknowledged: boolean) {
    if (hasSeverity(issues, "block") && !acknowledged) {
      const codes = issues.filter((i) => i.severity === "block").map((i) => i.code).join(", ");
      throw new ReviewError(`This reply has blocking issues (${codes}). Review them and confirm before sending.`, 422);
    }
  }

  approve(id: number, reviewer: string, acknowledged = false) {
    const d = this.pendingDraft(id);
    this.assertNoUnacknowledgedBlocks(d.guards, acknowledged);
    return this.finishReview(d, "approve", reviewer, d.reply_body, d.reply_subject, null);
  }

  sendEdited(id: number, reviewer: string, finalBody: string, finalSubject: string | null, reason: string | null, acknowledged = false) {
    const d = this.pendingDraft(id);
    if (!finalBody.trim()) throw new ReviewError("the reply is empty", 422);
    const recheck = checkDraft({
      draft: { ...d.draft, reply_body: finalBody, claims: [] },
      category: d.category, classification: d.classification, sources: d.knowledge.flatMap((l) => l.sources), kbStatus: d.kb_status, holdingReply: false, cfg: this.d.cfg,
    }).filter((g) => !["ungrounded_claim", "no_claims_recorded", "kb_insufficient", "kb_partial", "commitments_listed"].includes(g.code));
    this.assertNoUnacknowledgedBlocks(recheck, acknowledged);
    const decision = isUnchanged(d.reply_body, finalBody) ? "approve" : "edit";
    return this.finishReview(d, decision, reviewer, finalBody, finalSubject, reason);
  }

  reject(id: number, reviewer: string, reason: string, replacementBody: string | null) {
    const d = this.pendingDraft(id);
    return this.finishReview(d, "reject", reviewer, replacementBody?.trim() ? replacementBody : null, null, reason);
  }

  async escalate(id: number, reviewer: string, reason: string) {
    const d = this.pendingDraft(id);
    const email = this.getEmail(d.email_id)!;
    const result = this.finishReview(d, "escalate", reviewer, null, null, reason);
    let kbEscalation: unknown = null;
    try {
      kbEscalation = await this.d.kb.createEscalation(d.classification.product_questions[0] ?? email.subject, `Escalated from inbox by ${reviewer}: ${reason}`, `${email.subject}\n${email.body.slice(0, 1500)}`);
    } catch (e) {
      kbEscalation = { error: (e as Error).message };
    }
    return { ...result, kb_escalation: kbEscalation };
  }

  auditAutoSend(id: number, reviewer: string, ok: boolean, note: string | null) {
    const d = this.getDraft(id);
    if (!d || d.status !== "auto_sent") throw new ReviewError("not an autonomous send", 404);
    if (d.audit_status === "ok" || d.audit_status === "flagged") throw new ReviewError("already audited");
    this.d.db.prepare("UPDATE drafts SET audit_status=? WHERE id=?").run(ok ? "ok" : "flagged", id);
    this.d.feedback.record({ draftId: id, emailId: d.email_id, category: d.category, decision: ok ? "audit_ok" : "audit_flag", draftBody: d.reply_body, finalBody: d.reply_body, reason: note, reviewer, promptVersion: d.prompt_versions.draft, model: d.models.draft });
    const autonomy = this.d.autonomy.recordReview(d.category, ok ? "audit_ok" : "audit_flag", id, reviewer);
    this.d.audit.log(reviewer, ok ? "audit_ok" : "audit_flagged", "draft", id, note);
    return { draft: this.getDraft(id), autonomy };
  }
}
