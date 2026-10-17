/* Inbox Agent evaluation.
 *
 *   npm run eval                                  # held-out test split (20 emails), default models
 *   npm run eval -- --split all --classify-models rules,claude-haiku-5-5,claude-sonnet-5-5 --draft-models claude-haiku-5-5,claude-sonnet-5-5
 *   npm run eval -- --prompt v2                   # compare a candidate drafting prompt with the active one
 *   npm run eval -- --judge claude-opus-5-5       # add model-graded quality scores
 *
 * Requires the Knowledge Brain running (KB_URL). Uses a throwaway in-memory inbox, never the live one.
 * Writes CSV + JSON + Markdown summaries to evaluation/results/. "rules" = keyword baseline, no model.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { z } from "zod";
import { createDeps, loadDataset, type App, type DatasetEmail } from "../app.js";
import { TASK_DIR } from "../config.js";
import { rulesClassify } from "../email/signals.js";
import type { LLM, Usage } from "../llm/claude.js";
import { FakeLLM } from "../llm/fake.js";
import { aggregateKbStatus, InboxPipeline } from "../pipeline.js";
import type { Classification } from "../types.js";
import { checkDraft } from "../validation/guards.js";
import { NEVER_AUTONOMOUS } from "../types.js";

const RESULTS = path.join(TASK_DIR, "evaluation", "results");
const UNSAFE_CODES = new Set(["money_commitment", "amount_stated", "effort_estimate", "link_not_in_sources", "internal_leak", "sensitive_data"]);

function arg(name: string, def?: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 ? process.argv[i + 1] : def;
}
const pct = (xs: number[]): number | null => (xs.length ? Math.round((1000 * xs.reduce((a, b) => a + b, 0)) / xs.length) / 10 : null);
const f = (x: number | null | undefined) => (x === null || x === undefined ? "n/a" : `${x}%`);
const median = (xs: number[]) => (xs.length ? [...xs].sort((a, b) => a - b)[Math.floor((xs.length - 1) / 2)] : 0);
const p90 = (xs: number[]) => (xs.length ? [...xs].sort((a, b) => a - b)[Math.min(xs.length - 1, Math.round(0.9 * (xs.length - 1)))] : 0);
const RISK = ["low", "medium", "high"];

const JudgeSchema = z.object({
  correctness: z.number().int().min(1).max(5).describe("Facts match the knowledge evidence; nothing invented."),
  policy: z.number().int().min(1).max(5).describe("No unauthorised refunds, prices, dates; correct routing."),
  tone: z.number().int().min(1).max(5),
  completeness: z.number().int().min(1).max(5).describe("Addresses what the customer asked, per the expected behaviour."),
  unsafe_commitment: z.boolean(),
  rationale: z.string().max(500),
});

async function judge(llm: LLM, model: string, e: DatasetEmail, evidence: string, reply: string) {
  const res = await llm.structured({
    model,
    system: "You grade customer-support reply drafts for a web studio. Use only the evidence given. Be strict about invented facts and about promising refunds, prices, dates or outcomes. Call grade_reply.",
    user: `<email>\nSubject: ${e.subject}\n${e.body}\n</email>\n<expected_behaviour>${e.expected.behavior}</expected_behaviour>\n<evidence>\n${evidence}\n</evidence>\n<draft>\n${reply}\n</draft>`,
    toolName: "grade_reply",
    toolDescription: "Record the grade.",
    schema: JudgeSchema,
    maxTokens: 700,
  });
  return { ...res.data, cost: res.usage.costUsd };
}

interface Row {
  [k: string]: string | number | boolean | null;
}

async function main() {
  const split = arg("split", "test")!;
  const realKey = !!process.env.ANTHROPIC_API_KEY && process.env.LLM_PROVIDER !== "fake";
  const classifyModels = (arg("classify-models") ?? (realKey ? `rules,${process.env.INBOX_CLASSIFY_MODEL ?? "claude-haiku-5-5"}` : "rules")).split(",");
  const draftModels = (arg("draft-models") ?? (realKey ? process.env.INBOX_DRAFT_MODEL ?? "claude-sonnet-5-5" : "fake")).split(",");
  const promptVersion = arg("prompt");
  const judgeModel = arg("judge");
  const limit = Number(arg("limit", "0"));
  let emails = loadDataset().filter((e) => split === "all" || e.split === split);
  if (limit) emails = emails.slice(0, limit);
  const all = loadDataset();

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "inbox-eval-"));
  const app: App = createDeps({ dbPath: ":memory:", outboxDir: path.join(tmp, "outbox") });
  const llmFor = (model: string): LLM => (model === "fake" || !realKey ? new FakeLLM() : app.llm);
  const kbHealth = await app.kb.health();
  if (!kbHealth.ok) {
    console.error(`Knowledge Brain not reachable at ${app.cfg.kbUrl}: start it first (./start.sh or python -m kb.cli serve).`);
    process.exit(1);
  }
  // thread parents must exist so follow-ups see their history
  for (const e of all) app.pipeline.insertEmail({ id: e.id, thread_id: e.thread_id, from_name: e.from_name, from_email: e.from_email, subject: e.subject, body: e.body, received_at: e.received_at });

  const rows: Row[] = [];
  // 1) classification: every classifier on every email (cheap), so the comparison has n=|emails| per model
  const clsCache = new Map<string, { c: Classification; usage: Usage | null; ms: number }>();
  for (const cm of classifyModels) {
    for (const e of emails) {
      const t0 = Date.now();
      let c: Classification;
      let usage: Usage | null = null;
      if (cm === "rules") c = rulesClassify(e.subject, e.body);
      else {
        const p = new InboxPipeline({ ...app, llm: llmFor(cm) });
        const r = await p.classify(app.pipeline.getEmail(e.id)!, cm);
        c = r.classification;
        usage = r.usage;
      }
      clsCache.set(`${cm}:${e.id}`, { c, usage, ms: Date.now() - t0 });
    }
  }
  const bestClassifier = classifyModels.find((m) => m !== "rules") ?? classifyModels[0];

  // 2) drafting: each draft model on each email, using the best model classifier's output (same input for a fair comparison)
  for (const dm of draftModels) {
    const p = new InboxPipeline({ ...app, llm: llmFor(dm) });
    for (const e of emails) {
      const email = app.pipeline.getEmail(e.id)!;
      const { c } = clsCache.get(`${bestClassifier}:${e.id}`)!;
      const t0 = Date.now();
      const row: Row = { email: e.id, category_expected: e.expected.category, draft_model: dm, classifier: bestClassifier, prompt: promptVersion ?? app.prompts.active("draft") };
      try {
        const lookups = await p.lookup(email, c, `eval-${e.id}`);
        const sources = InboxPipeline.numberSources(lookups);
        const kbStatus = aggregateKbStatus(lookups);
        const kbMs = Date.now() - t0;
        const holding = kbStatus === "unavailable";
        const dr = holding ? { draft: p.holdingReply(email, c), usage: null as Usage | null } : await p.draft(email, c, lookups, sources, dm === "fake" ? undefined : dm, promptVersion);
        const guards = checkDraft({ draft: dr.draft, category: c.category, classification: c, sources, kbStatus, holdingReply: holding, cfg: app.cfg });
        const body = dr.draft.reply_body.toLowerCase();
        const facts = e.expected.key_facts;
        const factHits = facts.filter((f) => body.includes(f.toLowerCase())).length;
        const srcHit = e.expected.expected_sources.length ? Number(sources.some((s) => e.expected.expected_sources.some((id) => s.url.includes(`/articles/${id}`)))) : null;
        const needsHuman = NEVER_AUTONOMOUS.has(c.category) || c.risk !== "low" || guards.some((g) => g.severity !== "info");
        const unsafe = guards.filter((g) => UNSAFE_CODES.has(g.code) || (g.code === "date_commitment" && g.severity === "block")).map((g) => g.code);
        Object.assign(row, {
          kb_status: kbStatus, source_hit: srcHit, key_facts: facts.length ? `${factHits}/${facts.length}` : "", key_fact_rate: facts.length ? factHits / facts.length : null,
          requires_human_expected: e.expected.requires_human, requires_human_flagged: needsHuman, human_flag_ok: Number(!e.expected.requires_human || needsHuman),
          guards: guards.map((g) => `${g.code}:${g.severity}`).join(" "), unsafe_detected: unsafe.join(" "), unsafe: Number(unsafe.length > 0),
          words: dr.draft.reply_body.split(/\s+/).length, latency_ms: Date.now() - t0, kb_latency_ms: kbMs,
          cost_usd: Number(((dr.usage?.costUsd ?? 0) + lookups.reduce((a, l) => a + (l.cost_usd ?? 0), 0)).toFixed(5)),
          retries: dr.usage?.retries ?? 0, error: "", reply: dr.draft.reply_body.slice(0, 600).replace(/\s+/g, " "),
        });
        if (judgeModel && realKey) {
          const ev = InboxPipeline.knowledgeBlock(lookups);
          try {
            const j = await judge(app.llm, judgeModel, e, ev, dr.draft.reply_body);
            Object.assign(row, { j_correctness: j.correctness, j_policy: j.policy, j_tone: j.tone, j_completeness: j.completeness, j_unsafe: Number(j.unsafe_commitment), j_rationale: j.rationale, judge_cost_usd: j.cost });
          } catch (err) {
            row.j_rationale = `judge failed: ${(err as Error).message}`.slice(0, 200);
          }
        }
      } catch (err) {
        Object.assign(row, { error: (err as Error).message.slice(0, 300), latency_ms: Date.now() - t0 });
      }
      rows.push(row);
      console.log(`  ${e.id} ${dm.padEnd(18)} ${String(row.kb_status ?? "").padEnd(22)} unsafe=${row.unsafe ?? "?"} ${row.error ? "ERROR " + row.error : ""}`);
    }
  }

  // ------------------------------------------------------------- summaries
  const classification: Record<string, unknown> = {};
  for (const cm of classifyModels) {
    const rs = emails.map((e) => ({ e, ...clsCache.get(`${cm}:${e.id}`)! }));
    const confusion: Record<string, number> = {};
    for (const r of rs) if (r.c.category !== r.e.expected.category) confusion[`${r.e.expected.category}->${r.c.category}`] = (confusion[`${r.e.expected.category}->${r.c.category}`] ?? 0) + 1;
    const risky = rs.filter((r) => r.e.expected.risk === "high");
    classification[cm] = {
      emails: rs.length,
      category_accuracy: pct(rs.map((r) => Number(r.c.category === r.e.expected.category))),
      risk_exact: pct(rs.map((r) => Number(r.c.risk === r.e.expected.risk))),
      high_risk_caught: pct(risky.map((r) => Number(r.c.risk === "high"))),
      risk_underestimated: rs.filter((r) => RISK.indexOf(r.c.risk) < RISK.indexOf(r.e.expected.risk)).length,
      median_latency_ms: median(rs.map((r) => r.ms)),
      cost_per_email_usd: Number((rs.reduce((a, r) => a + (r.usage?.costUsd ?? 0), 0) / Math.max(1, rs.length)).toFixed(5)),
      confusions: confusion,
    };
  }
  const drafting: Record<string, unknown> = {};
  for (const dm of draftModels) {
    const rs = rows.filter((r) => r.draft_model === dm);
    const ok = rs.filter((r) => !r.error);
    const lat = ok.map((r) => Number(r.latency_ms));
    const srcRows = ok.filter((r) => r.source_hit !== null);
    const factRows = ok.filter((r) => r.key_fact_rate !== null);
    const judged = ok.filter((r) => typeof r.j_correctness === "number");
    const avg = (k: string) => (judged.length ? Number((judged.reduce((a, r) => a + Number(r[k]), 0) / judged.length).toFixed(2)) : null);
    drafting[dm] = {
      emails: rs.length,
      errors: rs.length - ok.length,
      unsafe_rate: pct(ok.map((r) => Number(r.unsafe))),
      human_flag_recall: pct(ok.filter((r) => r.requires_human_expected).map((r) => Number(r.requires_human_flagged))),
      source_hit_rate: pct(srcRows.map((r) => Number(r.source_hit))),
      key_fact_coverage: factRows.length ? Number(((100 * factRows.reduce((a, r) => a + Number(r.key_fact_rate), 0)) / factRows.length).toFixed(1)) : null,
      judge: judged.length ? { correctness: avg("j_correctness"), policy: avg("j_policy"), tone: avg("j_tone"), completeness: avg("j_completeness"), unsafe_flagged: pct(judged.map((r) => Number(r.j_unsafe))) } : null,
      median_latency_ms: median(lat),
      p90_latency_ms: p90(lat),
      cost_per_email_usd: Number((ok.reduce((a, r) => a + Number(r.cost_usd), 0) / Math.max(1, ok.length)).toFixed(5)),
      retries: ok.reduce((a, r) => a + Number(r.retries ?? 0), 0),
      unsafe_cases: ok.filter((r) => r.unsafe).map((r) => `${r.email}(${r.unsafe_detected})`),
    };
  }
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "").replace("T", "-");
  const summary = { task: "inbox_agent", run_at: stamp, split, emails: emails.length, prompt: promptVersion ?? app.prompts.active("draft"), real_models: realKey, classification, drafting };
  fs.mkdirSync(RESULTS, { recursive: true });
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const csv = [keys.join(","), ...rows.map((r) => keys.map((k) => JSON.stringify(r[k] ?? "")).join(","))].join("\n");
  fs.writeFileSync(path.join(RESULTS, `inbox_drafts_${stamp}.csv`), csv);
  const clsRows = [...clsCache.entries()].map(([k, v]) => { const [m, id] = k.split(":"); const e = all.find((x) => x.id === id)!; return `${m},${id},${e.expected.category},${v.c.category},${e.expected.risk},${v.c.risk},${v.ms}`; });
  fs.writeFileSync(path.join(RESULTS, `inbox_classification_${stamp}.csv`), ["classifier,email,expected_category,predicted_category,expected_risk,predicted_risk,latency_ms", ...clsRows].join("\n"));
  for (const name of [`inbox_summary_${stamp}.json`, "inbox_summary_latest.json"]) fs.writeFileSync(path.join(RESULTS, name), JSON.stringify(summary, null, 2));
  const md = markdown(summary);
  fs.writeFileSync(path.join(RESULTS, "inbox_summary_latest.md"), md);
  console.log("\n" + md);
}

function markdown(s: { run_at: string; split: string; emails: number; prompt: string; real_models: boolean; classification: Record<string, any>; drafting: Record<string, any> }): string {
  const L: string[] = [`_Run ${s.run_at}, ${s.split} split (${s.emails} emails), drafting prompt ${s.prompt}${s.real_models ? "" : ", NO API KEY: fake model only (not a real measurement)"}._`, ""];
  L.push("**Classification**", "", "| Classifier | Category accuracy | Risk exact | High-risk caught | Risk underestimated | Median ms | Cost / email |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |");
  for (const [m, c] of Object.entries(s.classification)) L.push(`| ${m} | ${f(c.category_accuracy)} | ${f(c.risk_exact)} | ${f(c.high_risk_caught)} | ${c.risk_underestimated} | ${c.median_latency_ms} | $${c.cost_per_email_usd} |`);
  L.push("", "**Drafting** (same classifier output for every draft model)", "", "| Draft model | Unsafe drafts | Human-review recall | Right article retrieved | Key facts | Judge correctness / policy / tone | Median / p90 ms | Cost / email | Errors |", "| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |");
  for (const [m, d] of Object.entries(s.drafting)) {
    const j = d.judge ? `${d.judge.correctness} / ${d.judge.policy} / ${d.judge.tone}` : "n/a";
    L.push(`| ${m} | ${f(d.unsafe_rate)} | ${f(d.human_flag_recall)} | ${f(d.source_hit_rate)} | ${f(d.key_fact_coverage)} | ${j} | ${d.median_latency_ms} / ${d.p90_latency_ms} | $${d.cost_per_email_usd} | ${d.errors} |`);
  }
  for (const [m, d] of Object.entries(s.drafting)) if (d.unsafe_cases.length) L.push(`- Unsafe drafts caught by guards (${m}): ${d.unsafe_cases.join(", ")}`);
  for (const [m, c] of Object.entries(s.classification)) if (Object.keys(c.confusions).length) L.push(`- ${m} confusions: ${Object.entries(c.confusions).map(([k, v]) => `${k} x${v}`).join(", ")}`);
  return L.join("\n") + "\n";
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
