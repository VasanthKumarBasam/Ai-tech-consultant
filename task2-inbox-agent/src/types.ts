import { z } from "zod";

export const CATEGORIES = [
  "how_to",
  "troubleshooting",
  "project_request",
  "refund_cancellation",
  "complaint",
  "quote_request",
  "follow_up",
  "other",
] as const;
export type Category = (typeof CATEGORIES)[number];

export const CATEGORY_LABEL: Record<Category, string> = {
  how_to: "How-to",
  troubleshooting: "Troubleshooting",
  project_request: "Project request",
  refund_cancellation: "Refund / cancellation",
  complaint: "Complaint",
  quote_request: "Quote request",
  follow_up: "Follow-up",
  other: "Other",
};

/** Categories that can never send without a human, whatever their approval streak. */
export const NEVER_AUTONOMOUS: ReadonlySet<Category> = new Set([
  "refund_cancellation",
  "complaint",
  "quote_request",
  "project_request",
]);

export const RISK = ["low", "medium", "high"] as const;
export type Risk = (typeof RISK)[number];

export const FLAGS = [
  "legal_threat",
  "chargeback_or_dispute",
  "security_or_account_access",
  "mentions_money",
  "mentions_deadline",
  "angry_customer",
  "possible_prompt_injection",
] as const;

export const ClassificationSchema = z.object({
  category: z.enum(CATEGORIES).describe("Primary intent of the email."),
  urgency: z.enum(["low", "normal", "high"]),
  risk: z.enum(RISK).describe("Business risk of replying incorrectly."),
  sentiment: z.enum(["positive", "neutral", "frustrated", "angry"]),
  summary: z.string().max(400).describe("One or two sentences: what the customer wants."),
  product_questions: z
    .array(z.string().max(300))
    .max(3)
    .describe("Self-contained product questions to look up in the knowledge base (empty if none)."),
  flags: z.array(z.enum(FLAGS)),
  customer_first_name: z.string().nullable(),
});
export type Classification = z.infer<typeof ClassificationSchema>;

export const DraftSchema = z.object({
  request_summary: z.string().max(500),
  reply_subject: z.string().max(200),
  reply_body: z.string().min(20).max(4000),
  claims: z
    .array(z.object({ text: z.string(), source_ids: z.array(z.string()) }))
    .describe("Every product fact stated in the reply, with the K# knowledge source ids that support it."),
  commitments: z
    .array(z.string())
    .describe("Every promise the reply makes (actions, dates, money). Empty if none."),
  next_action: z.string().max(300).describe("What our team should do next."),
  follow_up_date: z.string().nullable().describe("YYYY-MM-DD if a follow-up is needed, else null."),
});
export type Draft = z.infer<typeof DraftSchema>;

export interface InboundEmail {
  id: string;
  thread_id: string | null;
  from_name: string;
  from_email: string;
  subject: string;
  body: string;
  received_at: string;
}

export interface KnowledgeSource {
  id: string; // K1, K2 ... (stable within one draft)
  title: string;
  url: string;
  kind: string;
  section: string;
  snippet: string;
  image_url?: string | null;
}

export interface KnowledgeLookup {
  question: string;
  status: "answered" | "partial" | "insufficient_evidence" | "error" | "unavailable";
  answer: string;
  sources: KnowledgeSource[];
  escalation_id: number | null;
  request_id?: string;
  error?: string;
  latency_ms?: number;
  cost_usd?: number;
}

export interface GuardIssue {
  code: string;
  severity: "block" | "warn" | "info";
  message: string;
}

export type ReviewDecision = "approve" | "edit" | "reject" | "escalate";
