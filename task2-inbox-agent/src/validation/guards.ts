import type { Config } from "../config.js";
import type { Category, Classification, Draft, GuardIssue, KnowledgeSource } from "../types.js";

/**
 * Deterministic checks run on every draft after the model writes it.
 * "block" = must not go out without a human looking at this exact issue;
 * "warn"  = fine for a human to send, but disqualifies autonomous sending;
 * "info"  = shown to the reviewer only.
 */

const URL_RX = /https?:\/\/[^\s<>()"']+/gi;
const MONEY_PROMISE_RX =
  /\b(we|i)(?:'ll|'ve| will| have| are going to| can)\s+(?:also\s+|now\s+|already\s+|gladly\s+|happily\s+|go ahead and\s+)?(refund|credit|reimburse|waive|discount|cancel|process (?:your|the|a) refund|issue (?:you )?(?:a|the|your) refund)\b|\byour (refund|credit|cancellation) (has been|is being|will be|was) (processed|issued|approved|completed|confirmed)\b|\b(full|partial) refund (has been|will be|is)\b/i;
const AMOUNT_RX = /([$€£₹]\s?\d[\d,]*(\.\d+)?|\b\d[\d,]*(\.\d+)?\s?(usd|dollars|eur|gbp|inr)\b|\b\d{1,3}\s?%\s?(off|discount)\b)/i;
const DATE_RX =
  /\b(by|before|within|no later than|on)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today|tonight|end of (the )?(day|week|month)|\d+\s*(business\s+|working\s+)?(hours?|days?|weeks?))\b|\bwill be (done|fixed|live|ready|completed|resolved) (today|tomorrow|this week)\b/i;
const HOURS_RX = /\b\d+\s*(-|to)?\s*\d*\s*(hours|hrs|weeks|days)\s+(of (work|development)|to (build|complete|deliver))/i;
const INTERNAL_RX = /\[?\bK\d+\b\]?|knowledge base|as an ai\b|language model|system prompt|\bLLM\b/i;
const CARD_RX = /\b(?:\d[ -]?){13,19}\b/;
const SECRET_RX = /\b(password|passcode|api key|secret key)\s*[:=]\s*\S+/i;

const COMMERCIAL: ReadonlySet<Category> = new Set(["quote_request", "project_request", "refund_cancellation", "complaint"]);
const FACTUAL: ReadonlySet<Category> = new Set(["how_to", "troubleshooting"]);

function normUrl(u: string): string {
  try {
    const p = new URL(u.replace(/[.,;:!?)\]]+$/, ""));
    return `${p.hostname}${p.pathname.replace(/\/$/, "")}`.toLowerCase();
  } catch {
    return u.toLowerCase();
  }
}

export interface GuardInput {
  draft: Draft;
  category: Category;
  classification: Classification;
  sources: KnowledgeSource[];
  kbStatus: string;
  holdingReply: boolean;
  cfg: Config;
}

export function checkDraft(i: GuardInput): GuardIssue[] {
  const issues: GuardIssue[] = [];
  const body = i.draft.reply_body;
  const ids = new Set(i.sources.map((s) => s.id));
  const add = (code: string, severity: GuardIssue["severity"], message: string) => issues.push({ code, severity, message });

  if (i.holdingReply) add("kb_unavailable", "block", "Knowledge service was unavailable, so this is a holding reply with no product facts. Retry before answering the question.");
  else if (i.kbStatus === "insufficient_evidence" && FACTUAL.has(i.category))
    add("kb_insufficient", "warn", "The knowledge base could not answer this; an expert task was opened. Check the reply doesn't guess.");
  else if (i.kbStatus === "partial") add("kb_partial", "warn", "The knowledge base answered only part of the question.");
  else if (i.kbStatus === "error") add("kb_error", "warn", "The knowledge lookup failed; the reply has no verified facts.");

  for (const c of i.draft.claims) {
    const unknown = c.source_ids.filter((s) => !ids.has(s));
    if (unknown.length) add("unknown_source", "block", `Claim cites sources that were not retrieved (${unknown.join(", ")}): "${c.text.slice(0, 80)}"`);
    else if (!c.source_ids.length && FACTUAL.has(i.category)) add("ungrounded_claim", "warn", `Claim has no supporting source: "${c.text.slice(0, 100)}"`);
  }

  const allowed = new Set(i.sources.map((s) => normUrl(s.url)));
  for (const u of body.match(URL_RX) ?? []) {
    const host = (() => {
      try {
        return new URL(u).hostname;
      } catch {
        return "";
      }
    })();
    if (!allowed.has(normUrl(u)) && !i.cfg.allowedLinkDomains.some((d) => host === d || host.endsWith(`.${d}`)))
      add("link_not_in_sources", "block", `Link is not one of the retrieved sources: ${u}`);
  }

  if (MONEY_PROMISE_RX.test(body)) add("money_commitment", "block", "Reply appears to promise a refund, credit, discount or cancellation. Only a person can commit to that.");
  if (AMOUNT_RX.test(body)) add("amount_stated", COMMERCIAL.has(i.category) ? "block" : "warn", "Reply states a monetary amount or percentage.");
  if (DATE_RX.test(body)) add("date_commitment", COMMERCIAL.has(i.category) ? "block" : "warn", "Reply commits to a date or turnaround time.");
  if (HOURS_RX.test(body) && COMMERCIAL.has(i.category)) add("effort_estimate", "block", "Reply gives an effort estimate; estimates come from the scoping process.");
  if (INTERNAL_RX.test(body)) add("internal_leak", "block", "Reply mentions internal tooling (knowledge base, source ids, AI).");
  if (CARD_RX.test(body) || SECRET_RX.test(body)) add("sensitive_data", "block", "Reply contains something that looks like a card number or credential.");
  if (i.draft.commitments.length && COMMERCIAL.has(i.category))
    add("commitments_listed", "warn", `Model-reported commitments: ${i.draft.commitments.join("; ").slice(0, 300)}`);
  if (i.classification.flags.includes("possible_prompt_injection"))
    add("prompt_injection", "warn", "The email contains text that tries to instruct the assistant; check the reply follows policy.");
  if (body.length > 2500) add("too_long", "warn", `Reply is long (${body.length} characters).`);
  if (FACTUAL.has(i.category) && i.kbStatus === "answered" && !i.draft.claims.length)
    add("no_claims_recorded", "info", "No product facts were recorded for a knowledge-backed answer.");
  return issues;
}

export const hasSeverity = (issues: GuardIssue[], s: GuardIssue["severity"]) => issues.some((x) => x.severity === s);
