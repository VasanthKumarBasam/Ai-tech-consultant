import Anthropic from "@anthropic-ai/sdk";
import { z } from "zod";
import type { Config } from "../config.js";

export interface Usage {
  model: string;
  inputTokens: number;
  outputTokens: number;
  latencyMs: number;
  costUsd: number;
  retries: number;
}

export interface StructuredRequest<T extends z.ZodTypeAny> {
  model: string;
  system: string;
  user: string;
  toolName: string;
  toolDescription: string;
  schema: T;
  maxTokens?: number;
  /** Extra business validation; return an error message to make the model retry once. */
  validate?: (data: z.infer<T>) => string | null;
}

export interface LLM {
  provider: string;
  structured<T extends z.ZodTypeAny>(req: StructuredRequest<T>): Promise<{ data: z.infer<T>; usage: Usage }>;
}

export class LLMError extends Error {}

export function costFor(cfg: Config, model: string, inTok: number, outTok: number): number {
  const [pin, pout] = cfg.pricing[model] ?? [0, 0];
  return Math.round(((inTok / 1e6) * pin + (outTok / 1e6) * pout) * 1e6) / 1e6;
}

export function jsonSchemaFor(schema: z.ZodTypeAny): Record<string, unknown> {
  const js = z.toJSONSchema(schema) as Record<string, unknown>;
  delete js.$schema;
  return js;
}

/**
 * Claude via forced tool use: the model must return arguments matching the zod
 * schema. Schema errors and business-rule errors are fed back once as a
 * tool_result error so the model can correct itself; a second failure throws.
 */
export class ClaudeLLM implements LLM {
  provider = "anthropic";
  private client: Anthropic;
  private noTemperature = new Set<string>();

  constructor(private cfg: Config) {
    if (!process.env.ANTHROPIC_API_KEY) throw new LLMError("ANTHROPIC_API_KEY is not set");
    this.client = new Anthropic({ timeout: cfg.llmTimeoutMs, maxRetries: 3 });
  }

  async structured<T extends z.ZodTypeAny>(req: StructuredRequest<T>): Promise<{ data: z.infer<T>; usage: Usage }> {
    const tool = { name: req.toolName, description: req.toolDescription, input_schema: jsonSchemaFor(req.schema) as Anthropic.Tool.InputSchema };
    const messages: Anthropic.MessageParam[] = [{ role: "user", content: req.user }];
    const usage: Usage = { model: req.model, inputTokens: 0, outputTokens: 0, latencyMs: 0, costUsd: 0, retries: 0 };
    let lastErr = "";
    for (let attempt = 0; attempt < 2; attempt++) {
      const t0 = Date.now();
      const params: Anthropic.MessageCreateParamsNonStreaming = {
        model: req.model,
        max_tokens: req.maxTokens ?? 1500,
        system: [{ type: "text", text: req.system, cache_control: { type: "ephemeral" } }],
        messages,
        tools: [tool],
        tool_choice: { type: "tool", name: req.toolName },
      };
      if (!this.noTemperature.has(req.model)) params.temperature = 0;
      let resp: Anthropic.Message;
      try {
        resp = await this.client.messages.create(params);
      } catch (e) {
        const msg = String((e as Error).message ?? e);
        if (e instanceof Anthropic.BadRequestError && /temperature/i.test(msg) && params.temperature !== undefined) {
          this.noTemperature.add(req.model);
          delete params.temperature;
          resp = await this.client.messages.create(params);
        } else {
          throw new LLMError(`Claude API error: ${msg}`);
        }
      }
      const u = resp.usage;
      const inTok = (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0);
      usage.inputTokens += inTok;
      usage.outputTokens += u.output_tokens ?? 0;
      usage.latencyMs += Date.now() - t0;
      usage.costUsd += costFor(this.cfg, req.model, inTok, u.output_tokens ?? 0);
      const block = resp.content.find((b): b is Anthropic.ToolUseBlock => b.type === "tool_use");
      if (!block) {
        lastErr = "no tool call returned";
      } else {
        const parsed = req.schema.safeParse(block.input);
        if (parsed.success) {
          const bizErr = req.validate?.(parsed.data) ?? null;
          if (!bizErr) return { data: parsed.data, usage };
          lastErr = bizErr;
        } else {
          lastErr = parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ");
        }
      }
      usage.retries += 1;
      messages.push({ role: "assistant", content: resp.content });
      messages.push(
        block
          ? { role: "user", content: [{ type: "tool_result", tool_use_id: block.id, is_error: true, content: `Invalid: ${lastErr}. Call ${req.toolName} again with corrected input.` }] }
          : { role: "user", content: `You must call the ${req.toolName} tool.` },
      );
    }
    throw new LLMError(`Model output failed validation twice: ${lastErr}`);
  }
}

export class UnconfiguredLLM implements LLM {
  provider = "unconfigured";
  async structured<T extends z.ZodTypeAny>(): Promise<{ data: z.infer<T>; usage: Usage }> {
    throw new LLMError("ANTHROPIC_API_KEY is not set. Add it to .env (or run with LLM_PROVIDER=fake for a demo).");
  }
}
