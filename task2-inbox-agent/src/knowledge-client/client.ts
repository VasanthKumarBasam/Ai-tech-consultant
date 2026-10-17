import { z } from "zod";

/**
 * Typed client for the Knowledge Brain API (Task 1). The Inbox Agent never
 * touches Task 1's database or code: this HTTP contract is the only coupling.
 * Responses are validated at runtime so a contract change fails loudly here
 * instead of producing a silently wrong draft.
 */

export const KbSourceSchema = z.object({
  id: z.string(),
  cited: z.boolean(),
  title: z.string(),
  url: z.string(),
  section: z.string(),
  kind: z.enum(["text", "image", "expert"]),
  image_url: z.string().nullable().optional(),
  similarity: z.number(),
  snippet: z.string(),
  supporting_quote: z.string().nullable().optional(),
  chunk_id: z.string(),
});

export const KbQueryResponseSchema = z.object({
  request_id: z.string(),
  question: z.string(),
  status: z.enum(["answered", "partial", "insufficient_evidence", "error"]),
  answer: z.string(),
  sources: z.array(KbSourceSchema),
  needs_human: z.boolean(),
  escalation_id: z.number().nullable(),
  unsupported_points: z.array(z.string()).optional().default([]),
  reason: z.string().nullable().optional(),
  model: z.string(),
  prompt_version: z.string(),
  latency_ms: z.number(),
  usage: z.object({ cost_usd: z.number().optional().default(0) }).passthrough(),
  error: z.string().nullable().optional(),
});
export type KbQueryResponse = z.infer<typeof KbQueryResponseSchema>;
export type KbSource = z.infer<typeof KbSourceSchema>;

export class KnowledgeUnavailableError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
  }
}
export class KnowledgeRequestError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
  }
}

export interface QueryOptions {
  context?: string;
  requester?: string;
  requestId?: string;
  model?: string;
}

type FetchLike = typeof fetch;

export class KnowledgeClient {
  constructor(
    private baseUrl: string,
    private apiKey?: string,
    private timeoutMs = 60000,
    private fetchImpl: FetchLike = fetch,
    private retries = 1,
  ) {}

  private async request(path: string, init: RequestInit & { requestId?: string }): Promise<unknown> {
    let lastErr: unknown;
    for (let attempt = 0; attempt <= this.retries; attempt++) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
      try {
        const res = await this.fetchImpl(`${this.baseUrl}${path}`, {
          ...init,
          signal: ctrl.signal,
          headers: {
            "Content-Type": "application/json",
            ...(this.apiKey ? { "X-API-Key": this.apiKey } : {}),
            ...(init.requestId ? { "X-Request-ID": init.requestId } : {}),
          },
        });
        const body = await res.json().catch(() => ({}));
        if (res.status >= 500) {
          lastErr = new KnowledgeUnavailableError(`Knowledge Brain returned HTTP ${res.status}`);
        } else if (!res.ok) {
          const msg = (body as { error?: { message?: unknown } })?.error?.message;
          throw new KnowledgeRequestError(`Knowledge Brain rejected the request: ${JSON.stringify(msg ?? res.status)}`, res.status);
        } else {
          return body;
        }
      } catch (e) {
        if (e instanceof KnowledgeRequestError) throw e;
        lastErr = e;
      } finally {
        clearTimeout(timer);
      }
      if (attempt < this.retries) await new Promise((r) => setTimeout(r, 400 * (attempt + 1)));
    }
    const why = lastErr instanceof Error ? (lastErr.name === "AbortError" ? `timed out after ${this.timeoutMs} ms` : lastErr.message) : String(lastErr);
    throw new KnowledgeUnavailableError(`Knowledge Brain unavailable: ${why}`, lastErr);
  }

  async health(): Promise<{ ok: boolean; detail: unknown }> {
    try {
      const body = await this.request("/health", { method: "GET" });
      return { ok: true, detail: body };
    } catch (e) {
      return { ok: false, detail: (e as Error).message };
    }
  }

  /** Hand a question to a human expert through Task 1's escalation queue. */
  async createEscalation(question: string, reason: string, context?: string): Promise<{ id: number; deduplicated: boolean }> {
    const body = (await this.request("/escalations", {
      method: "POST",
      body: JSON.stringify({ question: question.slice(0, 2000), reason: reason.slice(0, 500), context: context?.slice(0, 6000), requester: "inbox-agent" }),
    })) as { id: number; deduplicated: boolean };
    return { id: body.id, deduplicated: body.deduplicated };
  }

  async query(question: string, opts: QueryOptions = {}): Promise<KbQueryResponse> {
    const body = await this.request("/query", {
      method: "POST",
      requestId: opts.requestId,
      body: JSON.stringify({
        question: question.slice(0, 2000),
        context: opts.context?.slice(0, 6000),
        requester: opts.requester ?? "inbox-agent",
        model: opts.model,
      }),
    });
    const parsed = KbQueryResponseSchema.safeParse(body);
    if (!parsed.success) {
      throw new KnowledgeUnavailableError(`Knowledge Brain response broke the contract: ${parsed.error.issues[0]?.message}`);
    }
    return parsed.data;
  }
}
