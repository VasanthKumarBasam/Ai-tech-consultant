import path from "node:path";
import { fileURLToPath } from "node:url";
import dotenv from "dotenv";

export const TASK_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
dotenv.config({ path: path.resolve(TASK_DIR, "..", ".env"), quiet: true } as dotenv.DotenvConfigOptions);

const num = (v: string | undefined, d: number) => (v === undefined || v === "" ? d : Number(v));

/** USD per million tokens [input, output]. Verify against the Claude pricing page before reporting. */
export const DEFAULT_PRICING: Record<string, [number, number]> = {
  "claude-opus-5-5": [4, 20],
  "claude-sonnet-5-5": [2, 10],
  "claude-haiku-5-5": [0.1, 0.5],
  fake: [0, 0],
  rules: [0, 0],
};

export interface Config {
  port: number;
  dbPath: string;
  outboxDir: string;
  kbUrl: string;
  kbApiKey?: string;
  kbTimeoutMs: number;
  llmProvider: "anthropic" | "fake";
  classifyModel: string;
  draftModel: string;
  learnModel: string;
  llmTimeoutMs: number;
  companyName: string;
  signature: string;
  allowedLinkDomains: string[];
  autonomy: { threshold: number; auditEvery: number; window: number; maxEditRate: number };
  pricing: Record<string, [number, number]>;
}

export function loadConfig(overrides: Partial<Config> = {}): Config {
  const pricing = { ...DEFAULT_PRICING };
  if (process.env.MODEL_PRICING_JSON) Object.assign(pricing, JSON.parse(process.env.MODEL_PRICING_JSON));
  const base: Config = {
    port: num(process.env.INBOX_PORT, 3002),
    dbPath: process.env.INBOX_DB_PATH ?? path.join(TASK_DIR, "data", "inbox.sqlite3"),
    outboxDir: process.env.INBOX_OUTBOX_DIR ?? path.join(TASK_DIR, "data", "outbox"),
    kbUrl: (process.env.KB_URL ?? "http://127.0.0.1:8001").replace(/\/$/, ""),
    kbApiKey: process.env.KB_API_KEY || undefined,
    kbTimeoutMs: num(process.env.INBOX_KB_TIMEOUT_MS, 60000),
    llmProvider: process.env.LLM_PROVIDER === "fake" ? "fake" : "anthropic",
    classifyModel: process.env.INBOX_CLASSIFY_MODEL ?? "claude-haiku-5-5",
    draftModel: process.env.INBOX_DRAFT_MODEL ?? "claude-sonnet-5-5",
    learnModel: process.env.INBOX_LEARN_MODEL ?? "claude-sonnet-5-5",
    llmTimeoutMs: num(process.env.INBOX_LLM_TIMEOUT_MS, 60000),
    companyName: process.env.COMPANY_NAME ?? "Northstar Web Studio",
    signature: process.env.INBOX_SIGNATURE ?? "Northstar Web Studio Support",
    allowedLinkDomains: (process.env.INBOX_ALLOWED_LINK_DOMAINS ?? "northstarweb.studio").split(",").map((s) => s.trim()).filter(Boolean),
    autonomy: {
      threshold: num(process.env.AUTONOMY_THRESHOLD, 20),
      auditEvery: num(process.env.AUTONOMY_AUDIT_EVERY, 5),
      window: num(process.env.AUTONOMY_WINDOW, 10),
      maxEditRate: num(process.env.AUTONOMY_MAX_EDIT_RATE, 0.2),
    },
    pricing,
  };
  return { ...base, ...overrides, autonomy: { ...base.autonomy, ...(overrides.autonomy ?? {}) } };
}
