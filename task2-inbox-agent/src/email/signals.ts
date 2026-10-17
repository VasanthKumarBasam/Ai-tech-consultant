import type { Category, Classification, Risk } from "../types.js";

/**
 * Deterministic signal detectors. They run on every email in addition to the
 * model's classification (defence in depth): a legal threat or chargeback is
 * flagged even if the model misses it. They also power the keyword baseline
 * classifier used in the model comparison.
 */
const RX = {
  legal_threat: /\b(lawyer|attorney|legal action|sue|lawsuit|solicitor|small claims)\b/i,
  chargeback_or_dispute: /\b(chargeback|charge ?back|dispute (the|this|it) (charge|payment)|bank dispute|report (you|this) to (my|the) bank)\b/i,
  security_or_account_access: /\b(password|hacked|hack|compromised|locked out|2fa|two[- ]factor|login (details|credentials)|data breach|phishing)\b/i,
  mentions_money: /(\$\s?\d|\b\d+\s?(usd|dollars)\b|\brefund|\bcharged?\b|\binvoice|\bprice|\bpricing|\bcost|\bfee\b|\bdiscount|\bbilling)/i,
  mentions_deadline: /\b(asap|urgent(ly)?|deadline|by (monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|end of (the )?(day|week|month))|launch (date|on)|before (our|the) (launch|event|conference))\b/i,
  angry_customer: /\b(unacceptable|furious|ridiculous|worst|disgusted|fed up|outraged|incompetent|terrible service)\b/i,
  possible_prompt_injection: /(ignore (all |any )?(previous|prior|above) (instructions|rules)|system prompt|you are now|disregard (your|the) (rules|instructions)|act as (an?|the) (admin|developer))/i,
} as const;

export function detectFlags(text: string): Classification["flags"] {
  return (Object.keys(RX) as (keyof typeof RX)[]).filter((k) => RX[k].test(text)) as Classification["flags"];
}

const CATEGORY_RULES: [Category, RegExp][] = [
  ["follow_up", /\b(follow(ing)?[- ]up|any update|still waiting|haven'?t heard|checking in|my (previous|earlier|last) (email|message)|sent (you )?an? (email|message) (last|on))\b/i],
  ["refund_cancellation", /\b(refund|money back|cancel (our|my|the) (plan|subscription|account|service|add-?on)|charged twice|double charged|chargeback|close (our|my) (account|directory|site)|downgrade)\b/i],
  ["complaint", /\b(unacceptable|disappointed|frustrated|furious|angry|terrible|broke our|broken our|lost (leads|business|data)|legal action|waited \d+ days|worst|not happy|unhappy)\b/i],
  ["quote_request", /\b(how much|quote|pricing|price for|cost (to|of|for)|estimate|ballpark|budget for)\b/i],
  ["project_request", /\b((could|can|would) you (build|add|create|develop|integrate|redesign|set up a new)|we('d| would) like (you )?to (build|add|create|integrate)|new feature|custom (feature|module|integration|wizard|app)|integrat(e|ion) with|redesign|mobile app)\b/i],
  ["troubleshooting", /\b(not working|doesn'?t work|isn'?t working|won'?t|error|failed|fails|broken|no results|not secure|wrong (order|results)|not (showing|arriving|loading)|refused|rejected|stopped)\b/i],
  ["how_to", /\b(how (do|can|should) (i|we)|how to|where (do|can) (i|we)|is it possible|can (i|we)|steps to)\b/i],
];

const DEFAULT_RISK: Record<Category, Risk> = {
  how_to: "low",
  troubleshooting: "medium",
  project_request: "medium",
  refund_cancellation: "high",
  complaint: "high",
  quote_request: "medium",
  follow_up: "medium",
  other: "medium",
};

export function rulesCategory(text: string): Category {
  for (const [cat, rx] of CATEGORY_RULES) if (rx.test(text)) return cat;
  return "other";
}

/** Keyword baseline classifier (no model). Used as a comparison point in evals and by FakeLLM. */
export function rulesClassify(subject: string, body: string): Classification {
  const text = `${subject}\n${body}`;
  const category = rulesCategory(text);
  const flags = detectFlags(text);
  const question = body.split(/(?<=[.?!])\s+/).find((s) => s.includes("?")) ?? subject;
  return {
    category,
    urgency: flags.includes("mentions_deadline") || flags.includes("angry_customer") ? "high" : "normal",
    risk: minRisk(category, flags, DEFAULT_RISK[category]),
    sentiment: flags.includes("angry_customer") ? "angry" : category === "complaint" ? "frustrated" : "neutral",
    summary: subject,
    product_questions: ["how_to", "troubleshooting"].includes(category) ? [question.slice(0, 300)] : [],
    flags,
    customer_first_name: null,
  };
}

/** Risk can only be raised by deterministic signals, never lowered. */
export function minRisk(category: Category, flags: Classification["flags"], modelRisk: Risk): Risk {
  const order: Risk[] = ["low", "medium", "high"];
  let r = Math.max(order.indexOf(modelRisk), order.indexOf(DEFAULT_RISK[category] === "high" ? "high" : "low"));
  if (flags.some((f) => ["legal_threat", "chargeback_or_dispute", "security_or_account_access"].includes(f))) r = 2;
  if (flags.includes("possible_prompt_injection") || flags.includes("angry_customer")) r = Math.max(r, 1);
  return order[r];
}
