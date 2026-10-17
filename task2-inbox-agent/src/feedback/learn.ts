import fs from "node:fs";
import path from "node:path";
import { z } from "zod";
import type { App } from "../app.js";
import { listVersionsOnDisk, loadGuidelines, PROMPT_DIR } from "../drafting/prompts.js";

/**
 * Feedback learning without retraining: recurring reviewer edits are turned
 * into a *candidate* prompt version (new style guidelines). Nothing changes in
 * production until the candidate beats the active version on the fixed eval set
 * and a person activates it; the old version stays available for rollback.
 */

const ProposalSchema = z.object({
  analysis: z.string().max(1500).describe("What the reviewers consistently changed, with counts where possible."),
  new_guidelines: z.array(z.string().max(220)).min(1).max(8).describe("Short, general, imperative style/policy rules."),
  expected_effect: z.string().max(400),
});

const UNSAFE_GUIDELINE = /\b(promise|guarantee|offer|confirm)\b.*\b(refund|discount|credit|price|deadline|date)\b|\bskip\b.*\b(source|citation|review)\b|\bwithout (checking|sources)\b/i;

const SYSTEM = `You improve the style guidelines of a customer-support drafting prompt using evidence from human reviewers.
You receive pairs of (model draft, reviewer's final version) with the reviewer's stated reason.
Propose a few general guidelines that would have made the drafts closer to the final versions.
Rules:
- Guidelines must be general (apply to future emails), short and imperative.
- Never include customer-specific facts, names, products or prices.
- Never propose anything that weakens grounding (only state facts from sources) or the ban on promising refunds, prices, dates or deadlines.
- Prefer patterns seen in several edits over one-offs.
Call propose_prompt_revision.`;

export async function proposeRevision(app: App, minEdits = 3) {
  const active = app.prompts.active("draft");
  const edits = app.feedback.editsForLearning(active, 40);
  if (edits.length < minEdits) {
    throw new Error(`Only ${edits.length} edited/replaced drafts recorded for draft.${active}; need at least ${minEdits} to learn from.`);
  }
  const byCat: Record<string, { n: number; ratio: number }> = {};
  for (const e of edits) {
    byCat[e.category] ??= { n: 0, ratio: 0 };
    byCat[e.category].n++;
    byCat[e.category].ratio += e.edit_ratio;
  }
  const pairs = edits
    .slice(0, 20)
    .map((e, i) => `<edit n="${i + 1}" category="${e.category}" reason="${(e.reason ?? "none given").replace(/"/g, "'")}" edit_ratio="${e.edit_ratio}">\n<draft>\n${e.draft_body}\n</draft>\n<final>\n${e.final_body}\n</final>\n</edit>`)
    .join("\n");
  const user = `<current_guidelines>\n${loadGuidelines(active)}\n</current_guidelines>\n<edit_stats>\n${JSON.stringify(byCat)}\n</edit_stats>\n${pairs}\nPropose revised guidelines.`;
  const res = await app.llm.structured({
    model: app.cfg.learnModel,
    system: SYSTEM,
    user,
    toolName: "propose_prompt_revision",
    toolDescription: "Propose new style guidelines learned from reviewer edits.",
    schema: ProposalSchema,
    maxTokens: 1200,
    validate: (d) => {
      const bad = d.new_guidelines.filter((g) => UNSAFE_GUIDELINE.test(g));
      return bad.length ? `These guidelines weaken safety rules and are not allowed: ${bad.join(" | ")}` : null;
    },
  });
  const proposal = res.data;
  const safe = proposal.new_guidelines.filter((g) => !UNSAFE_GUIDELINE.test(g));
  const existing = loadGuidelines(active)
    .split("\n")
    .filter((l) => l.trim() && !l.includes("(none"));
  const next = `v${Math.max(...listVersionsOnDisk("draft").map((v) => Number(v.slice(1)))) + 1}`;
  fs.copyFileSync(path.join(PROMPT_DIR, `draft.${active}.md`), path.join(PROMPT_DIR, `draft.${next}.md`));
  const guidelines = [...existing, ...safe.map((g) => `- ${g.replace(/^[-*]\s*/, "")}`)].join("\n");
  fs.writeFileSync(path.join(PROMPT_DIR, `draft.${next}.guidelines.md`), guidelines + "\n");
  const notes = `learned from ${edits.length} edits on draft.${active}: ${proposal.expected_effect}`.slice(0, 500);
  app.prompts.register("draft", next, notes);
  app.audit.log("learn", "prompt_candidate_created", "prompt", `draft.${next}`, { from: active, edits: edits.length, guidelines: safe });
  return { from: active, version: next, analysis: proposal.analysis, guidelines: safe, expected_effect: proposal.expected_effect, cost_usd: res.usage.costUsd };
}
