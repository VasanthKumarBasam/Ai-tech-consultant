import os from "node:os";
import path from "node:path";
import fs from "node:fs";
import { createDeps, type App } from "../src/app.js";
import { KnowledgeClient } from "../src/knowledge-client/client.js";
import { FakeLLM } from "../src/llm/fake.js";

export const REFUND_URL = "https://support.brilliantdirectories.com/support/solutions/articles/5000674165-how-refunds-work";
export const GATEWAY_URL = "https://support.brilliantdirectories.com/support/solutions/articles/5000673830-finance-payment-settings-setting-up-a-payment-gateway";

export type KbMode = "answer" | "insufficient" | "down" | "slow" | "broken";

export function kbResponse(question: string, status: "answered" | "insufficient_evidence" = "answered") {
  const answered = status === "answered";
  return {
    request_id: "kb-1",
    question,
    status,
    answer: answered ? "Navigate to Finance » Payment Settings and choose the gateway in the Add New Payment Gateway drop-down. [S1]" : "Not covered.",
    sources: answered
      ? [{ id: "S1", cited: true, title: "Setting Up a Payment Gateway", url: GATEWAY_URL, section: "Add a gateway", kind: "text", image_url: null, similarity: 0.82, snippet: "Navigate to Finance » Payment Settings…", supporting_quote: "Navigate to Finance » Payment Settings", chunk_id: "c1" }]
      : [],
    images: [],
    needs_human: !answered,
    escalation_id: answered ? null : 7,
    unsupported_points: [],
    reason: null,
    model: "fake",
    prompt_version: "answer-v2",
    retrieval: { mode: "hybrid", top_similarity: 0.8, min_similarity: 0.5, candidates: 5 },
    usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0.001 },
    latency_ms: 12,
  };
}

export function stubFetch(state: { mode: KbMode; calls: number }) {
  return (async (url: string | URL, init?: RequestInit) => {
    state.calls++;
    const u = String(url);
    if (state.mode === "down") throw new TypeError("fetch failed: ECONNREFUSED");
    if (state.mode === "slow") {
      await new Promise((resolve, reject) => {
        const t = setTimeout(resolve, 5000);
        init?.signal?.addEventListener("abort", () => { clearTimeout(t); const e = new Error("aborted"); e.name = "AbortError"; reject(e); });
      });
    }
    if (u.endsWith("/health")) return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
    if (u.endsWith("/escalations")) return new Response(JSON.stringify({ id: 42, deduplicated: false }), { status: 201 });
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (state.mode === "broken") return new Response(JSON.stringify({ hello: "world" }), { status: 200 });
    return new Response(JSON.stringify(kbResponse(body.question, state.mode === "insufficient" ? "insufficient_evidence" : "answered")), { status: 200 });
  }) as typeof fetch;
}

export function makeApp(mode: KbMode = "answer", overrides: Parameters<typeof createDeps>[0] = {}) {
  const state = { mode, calls: 0 };
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "inbox-"));
  const kb = new KnowledgeClient("http://kb.test", undefined, 200, stubFetch(state), 1);
  const app: App = createDeps({ dbPath: ":memory:", outboxDir: path.join(dir, "outbox"), llmProvider: "fake", ...overrides }, { llm: new FakeLLM(), kb });
  return { app, state };
}

let n = 0;
export function addEmail(app: App, subject: string, body: string, extra: Partial<{ id: string; thread_id: string; from_name: string }> = {}) {
  n++;
  return app.pipeline.insertEmail({
    id: extra.id ?? `T${n}-${Math.random().toString(36).slice(2, 7)}`,
    thread_id: extra.thread_id ?? null,
    from_name: extra.from_name ?? "Dana Whitfield",
    from_email: "dana@example.com",
    subject,
    body,
    received_at: new Date(Date.now() + n * 1000).toISOString(),
  });
}
