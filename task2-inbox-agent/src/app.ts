import fs from "node:fs";
import path from "node:path";
import { Audit } from "./audit.js";
import { Autonomy } from "./autonomy/autonomy.js";
import { loadConfig, TASK_DIR, type Config } from "./config.js";
import { openDb } from "./db.js";
import { PromptRegistry } from "./drafting/prompts.js";
import { FeedbackStore } from "./feedback/feedback.js";
import { KnowledgeClient } from "./knowledge-client/client.js";
import { ClaudeLLM, UnconfiguredLLM, type LLM } from "./llm/claude.js";
import { FakeLLM } from "./llm/fake.js";
import { Outbox } from "./outbox/outbox.js";
import { InboxPipeline, type Deps } from "./pipeline.js";
import type { InboundEmail } from "./types.js";

export interface App extends Deps {
  pipeline: InboxPipeline;
}

export function buildLLM(cfg: Config): LLM {
  if (cfg.llmProvider === "fake") return new FakeLLM();
  if (!process.env.ANTHROPIC_API_KEY) return new UnconfiguredLLM();
  return new ClaudeLLM(cfg);
}

export function createDeps(overrides: Partial<Config> = {}, inject: Partial<Pick<Deps, "llm" | "kb">> = {}): App {
  const cfg = loadConfig(overrides);
  const db = openDb(cfg.dbPath);
  const deps: Deps = {
    cfg,
    db,
    llm: inject.llm ?? buildLLM(cfg),
    kb: inject.kb ?? new KnowledgeClient(cfg.kbUrl, cfg.kbApiKey, cfg.kbTimeoutMs),
    prompts: new PromptRegistry(db),
    feedback: new FeedbackStore(db),
    autonomy: new Autonomy(db, cfg),
    outbox: new Outbox(db, cfg.outboxDir),
    audit: new Audit(db),
  };
  return { ...deps, pipeline: new InboxPipeline(deps) };
}

export interface DatasetEmail extends InboundEmail {
  split: "dev" | "test";
  expected: {
    category: string;
    risk: string;
    requires_human: boolean;
    kb_needed: boolean;
    expected_sources: string[];
    key_facts: string[];
    must_not: string[];
    behavior: string;
  };
}

export function loadDataset(): DatasetEmail[] {
  return JSON.parse(fs.readFileSync(path.join(TASK_DIR, "data", "emails.json"), "utf8")).emails;
}

/** Loads the 50-email dataset into the inbox (idempotent). Received times are spread over the last days. */
export function seedInbox(app: App, which: "all" | "dev" | "test" = "all"): number {
  const emails = loadDataset().filter((e) => which === "all" || e.split === which);
  let n = 0;
  for (const e of emails) {
    const before = app.pipeline.getEmail(e.id);
    app.pipeline.insertEmail({ id: e.id, thread_id: e.thread_id, from_name: e.from_name, from_email: e.from_email, subject: e.subject, body: e.body, received_at: e.received_at });
    if (!before) n++;
  }
  return n;
}
