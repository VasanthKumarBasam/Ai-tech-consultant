import type { z } from "zod";
import { rulesClassify } from "../email/signals.js";
import type { LLM, StructuredRequest, Usage } from "./claude.js";

/**
 * Deterministic stand-in for Claude (LLM_PROVIDER=fake): used by unit tests and
 * for clicking through the UI without an API key. Classification uses the
 * keyword baseline; drafts are assembled from the knowledge answers in the
 * prompt. The UI labels every fake draft so it is never mistaken for Claude.
 */
export class FakeLLM implements LLM {
  provider = "fake";
  calls: { tool: string; user: string }[] = [];
  overrides: Record<string, (user: string) => unknown> = {};

  async structured<T extends z.ZodTypeAny>(req: StructuredRequest<T>): Promise<{ data: z.infer<T>; usage: Usage }> {
    this.calls.push({ tool: req.toolName, user: req.user });
    const handler = this.overrides[req.toolName] ?? HANDLERS[req.toolName];
    if (!handler) throw new Error(`FakeLLM: no handler for ${req.toolName}`);
    const data = req.schema.parse(handler(req.user));
    const err = req.validate?.(data);
    if (err) throw new Error(`Model output failed validation twice: ${err}`);
    return { data, usage: { model: "fake", inputTokens: Math.round(req.user.length / 4), outputTokens: 120, latencyMs: 1, costUsd: 0, retries: 0 } };
  }
}

const tag = (user: string, name: string) => user.match(new RegExp(`<${name}>([\\s\\S]*?)</${name}>`))?.[1]?.trim() ?? "";

const HANDLERS: Record<string, (user: string) => unknown> = {
  classify_email(user) {
    const email = tag(user, "customer_email");
    const subject = email.match(/^Subject: (.*)$/m)?.[1] ?? "";
    const body = email.split(/\n\n/).slice(1).join("\n\n");
    const name = email.match(/^From: ([A-Za-z]+)/m)?.[1] ?? null;
    return { ...rulesClassify(subject, body), customer_first_name: name };
  },
  draft_reply(user) {
    const cls = JSON.parse(tag(user, "classification") || "{}");
    const email = tag(user, "customer_email");
    const subject = email.match(/^Subject: (.*)$/m)?.[1] ?? "your message";
    const knowledge = tag(user, "knowledge");
    const answers = [...knowledge.matchAll(/<answer status="answered">([\s\S]*?)<\/answer>/g)].map((m) => m[1].trim());
    const srcs = [...knowledge.matchAll(/<source id="(K\d+)" title="([^"]*)" url="([^"]*)"/g)].map((m) => ({ id: m[1], title: m[2], url: m[3] }));
    const name = cls.customer_first_name ? ` ${cls.customer_first_name}` : "";
    const factual = ["how_to", "troubleshooting"].includes(cls.category);
    let middle: string;
    let claims: { text: string; source_ids: string[] }[] = [];
    if (factual && answers.length && srcs.length) {
      const fact = answers[0].replace(/\[S\d+\]/g, "").replace(/\[FAKE MODEL\]\s*/g, "").split(/(?<=[.!?])\s+/)[0];
      middle = `[DEMO DRAFT] Here is what our documentation says: ${fact}\n\nFull guide: ${srcs[0].url}`;
      claims = [{ text: fact, source_ids: [srcs[0].id] }];
    } else if (factual) {
      middle = "[DEMO DRAFT] I'm checking this with one of our platform specialists and will come back to you with the exact steps.";
    } else {
      middle = `[DEMO DRAFT] I've passed your message about "${subject}" to the right person on our team, and they will follow up with you directly.`;
    }
    return {
      request_summary: cls.summary ?? subject,
      reply_subject: `Re: ${subject}`,
      reply_body: `Hi${name},\n\nThanks for getting in touch.\n\n${middle}\n\nBest regards,\nNorthstar Web Studio Support`,
      claims,
      commitments: factual ? [] : ["a team member will follow up"],
      next_action: factual ? "Confirm the customer resolved it." : "Route to the account owner.",
      follow_up_date: null,
    };
  },
  propose_prompt_revision() {
    return {
      analysis: "Fake model: reviewers shortened replies and removed filler phrases.",
      new_guidelines: ["Keep replies under 120 words.", "Open with the answer, not with thanks."],
      expected_effect: "Fewer edits on how_to replies.",
    };
  },
  judge_reply() {
    return { correctness: 3, grounding: 3, tone: 3, policy: 3, completeness: 3, unsafe_commitment: false, rationale: "fake judge" };
  },
};
