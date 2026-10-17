import path from "node:path";
import { fileURLToPath } from "node:url";
import express, { type NextFunction, type Request, type Response } from "express";
import { z } from "zod";
import { createDeps, seedInbox, type App } from "./app.js";
import { TASK_DIR } from "./config.js";
import { ProcessingError, ReviewError } from "./pipeline.js";
import { CATEGORIES, CATEGORY_LABEL, NEVER_AUTONOMOUS, type Category } from "./types.js";

const Reviewer = z.string().trim().min(1, "reviewer name is required").max(100);

const schemas = {
  newEmail: z.object({
    from_name: z.string().min(1).max(200),
    from_email: z.string().email(),
    subject: z.string().min(1).max(300),
    body: z.string().min(1).max(20000),
    thread_id: z.string().max(100).nullable().optional(),
  }),
  approve: z.object({ reviewer: Reviewer, acknowledged: z.boolean().optional().default(false) }),
  edit: z.object({
    reviewer: Reviewer,
    body: z.string().min(1).max(8000),
    subject: z.string().max(300).nullable().optional(),
    reason: z.string().max(500).nullable().optional(),
    acknowledged: z.boolean().optional().default(false),
  }),
  reject: z.object({ reviewer: Reviewer, reason: z.string().min(1).max(500), replacement: z.string().max(8000).nullable().optional() }),
  escalate: z.object({ reviewer: Reviewer, reason: z.string().min(1).max(500) }),
  audit: z.object({ reviewer: Reviewer, ok: z.boolean(), note: z.string().max(500).nullable().optional() }),
  toggle: z.object({ actor: Reviewer, reason: z.string().max(300).optional() }),
  activate: z.object({ name: z.enum(["classify", "draft"]), version: z.string().regex(/^v\d+$/), actor: Reviewer }),
};

type Handler = (req: Request, res: Response) => unknown | Promise<unknown>;
const h = (fn: Handler) => (req: Request, res: Response, next: NextFunction) => Promise.resolve(fn(req, res)).catch(next);

export function createServer(app: App) {
  const server = express();
  server.use(express.json({ limit: "256kb" }));
  server.use(express.static(path.join(TASK_DIR, "public")));
  const { pipeline, db } = app;
  let batch: { running: boolean; done: number; total: number; errors: number } = { running: false, done: 0, total: 0, errors: 0 };

  server.get("/api/health", h(async (_req, res) => {
    const kb = await app.kb.health();
    const counts = db.prepare("SELECT status, COUNT(*) n FROM emails GROUP BY status").all();
    res.json({
      ok: true,
      llm_provider: app.llm.provider,
      models: { classify: app.cfg.classifyModel, draft: app.cfg.draftModel },
      prompts: { classify: app.prompts.active("classify"), draft: app.prompts.active("draft") },
      kb: { url: app.cfg.kbUrl, ok: kb.ok, detail: kb.ok ? undefined : kb.detail },
      counts,
      batch,
    });
  }));

  server.get("/api/emails", h((req, res) => {
    const status = typeof req.query.status === "string" ? req.query.status : null;
    const rows = db
      .prepare(
        `SELECT e.id, e.thread_id, e.from_name, e.from_email, e.subject, e.received_at, e.status, e.category, e.last_error,
                d.id AS draft_id, d.risk, d.status AS draft_status, d.requires_approval, d.holding_reply, d.kb_status, d.guards_json, d.audit_status
         FROM emails e LEFT JOIN drafts d ON d.id = (SELECT MAX(id) FROM drafts WHERE email_id = e.id)
         ${status ? "WHERE e.status = ?" : ""}
         ORDER BY e.received_at DESC`,
      )
      .all(...(status ? [status] : [])) as Record<string, unknown>[];
    res.json({
      emails: rows.map(({ guards_json, ...r }) => {
        const g = guards_json ? (JSON.parse(String(guards_json)) as { severity: string }[]) : [];
        return { ...r, blocks: g.filter((x) => x.severity === "block").length, warnings: g.filter((x) => x.severity === "warn").length };
      }),
    });
  }));

  server.post("/api/emails", h((req, res) => {
    const b = schemas.newEmail.parse(req.body);
    const n = (db.prepare("SELECT COUNT(*) n FROM emails WHERE id LIKE 'M-%'").get() as { n: number }).n + 1;
    const email = pipeline.insertEmail({ id: `M-${String(n).padStart(3, "0")}`, thread_id: b.thread_id ?? null, from_name: b.from_name, from_email: b.from_email, subject: b.subject, body: b.body });
    res.status(201).json(email);
  }));

  server.post("/api/seed", h((req, res) => {
    const which = (req.body?.which ?? "all") as "all" | "dev" | "test";
    res.json({ inserted: seedInbox(app, which) });
  }));

  server.post("/api/process-new", h((_req, res) => {
    if (batch.running) return res.status(409).json({ error: { code: "conflict", message: "a batch is already running" } });
    const ids = (db.prepare("SELECT id FROM emails WHERE status IN ('new','error') ORDER BY received_at").all() as { id: string }[]).map((r) => r.id);
    batch = { running: true, done: 0, total: ids.length, errors: 0 };
    (async () => {
      const queue = [...ids];
      const worker = async () => {
        for (let id = queue.shift(); id; id = queue.shift()) {
          try {
            await pipeline.process(id);
          } catch {
            batch.errors++;
          }
          batch.done++;
        }
      };
      await Promise.all([worker(), worker()]);
      batch.running = false;
    })();
    res.status(202).json(batch);
  }));

  server.get("/api/emails/:id", h((req, res) => {
    const email = pipeline.getEmail(String(req.params.id));
    if (!email) return res.status(404).json({ error: { code: "not_found", message: "email not found" } });
    const drafts = (db.prepare("SELECT id, status, created_at, reviewer FROM drafts WHERE email_id=? ORDER BY id DESC").all(email.id));
    const sent = db.prepare("SELECT * FROM sent_messages WHERE email_id=?").get(email.id) ?? null;
    res.json({ email, draft: pipeline.latestDraft(email.id) ?? null, drafts, sent, history: pipeline.threadHistory(email) });
  }));

  server.post("/api/emails/:id/process", h(async (req, res) => {
    res.json(await pipeline.process(String(req.params.id), { actor: "reviewer" }));
  }));

  server.post("/api/drafts/:id/approve", h((req, res) => {
    const b = schemas.approve.parse(req.body);
    res.json(pipeline.approve(Number(req.params.id), b.reviewer, b.acknowledged));
  }));
  server.post("/api/drafts/:id/send-edited", h((req, res) => {
    const b = schemas.edit.parse(req.body);
    res.json(pipeline.sendEdited(Number(req.params.id), b.reviewer, b.body, b.subject ?? null, b.reason ?? null, b.acknowledged));
  }));
  server.post("/api/drafts/:id/reject", h((req, res) => {
    const b = schemas.reject.parse(req.body);
    res.json(pipeline.reject(Number(req.params.id), b.reviewer, b.reason, b.replacement ?? null));
  }));
  server.post("/api/drafts/:id/escalate", h(async (req, res) => {
    const b = schemas.escalate.parse(req.body);
    res.json(await pipeline.escalate(Number(req.params.id), b.reviewer, b.reason));
  }));
  server.post("/api/drafts/:id/audit", h((req, res) => {
    const b = schemas.audit.parse(req.body);
    res.json(pipeline.auditAutoSend(Number(req.params.id), b.reviewer, b.ok, b.note ?? null));
  }));

  server.get("/api/autonomy", h((_req, res) => {
    const auditQueue = db
      .prepare("SELECT d.id, d.email_id, d.category, d.reply_subject, d.reply_body, d.created_at FROM drafts d WHERE d.status='auto_sent' AND d.audit_status='pending' ORDER BY d.id DESC")
      .all();
    res.json({
      threshold: app.cfg.autonomy.threshold,
      categories: app.autonomy.all().map((s) => ({ ...s, label: CATEGORY_LABEL[s.category] })),
      events: app.autonomy.events(60),
      audit_queue: auditQueue,
    });
  }));
  server.post("/api/autonomy/:category/enable", h((req, res) => {
    const b = schemas.toggle.parse(req.body);
    const cat = z.enum(CATEGORIES).parse(req.params.category) as Category;
    if (NEVER_AUTONOMOUS.has(cat)) throw new ReviewError(`${CATEGORY_LABEL[cat]} always requires a person`, 422);
    try {
      const s = app.autonomy.enable(cat, b.actor);
      app.audit.log(b.actor, "autonomy_enabled", "category", cat);
      res.json(s);
    } catch (e) {
      throw new ReviewError((e as Error).message, 422);
    }
  }));
  server.post("/api/autonomy/:category/disable", h((req, res) => {
    const b = schemas.toggle.parse(req.body);
    const cat = z.enum(CATEGORIES).parse(req.params.category) as Category;
    const s = app.autonomy.disable(cat, b.reason ?? `disabled by ${b.actor}`, b.actor);
    app.audit.log(b.actor, "autonomy_disabled", "category", cat, b.reason);
    res.json(s);
  }));

  server.get("/api/feedback", h((_req, res) => res.json({ stats: app.feedback.stats(), recent: app.feedback.recent(40) })));
  server.get("/api/prompts", h((_req, res) => res.json({ versions: app.prompts.list() })));
  server.post("/api/prompts/activate", h((req, res) => {
    const b = schemas.activate.parse(req.body);
    app.prompts.activate(b.name, b.version);
    app.audit.log(b.actor, "prompt_activated", "prompt", `${b.name}.${b.version}`);
    res.json({ versions: app.prompts.list() });
  }));
  server.get("/api/audit", h((_req, res) => res.json({ entries: app.audit.recent(150) })));
  server.get("/api/outbox", h((_req, res) => res.json({ sent: app.outbox.list(100) })));

  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  server.use((err: unknown, _req: Request, res: Response, _next: NextFunction) => {
    if (err instanceof z.ZodError) return res.status(422).json({ error: { code: "invalid_request", message: err.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ") } });
    if (err instanceof ReviewError) return res.status(err.status).json({ error: { code: err.status === 404 ? "not_found" : "review_blocked", message: err.message } });
    if (err instanceof ProcessingError) return res.status(502).json({ error: { code: "processing_failed", message: err.message } });
    console.error(err);
    res.status(500).json({ error: { code: "internal_error", message: (err as Error).message } });
  });
  return server;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const app = createDeps();
  const n = seedInbox(app, "all");
  createServer(app).listen(app.cfg.port, "127.0.0.1", () => {
    console.log(`Inbox Agent on http://localhost:${app.cfg.port}  (llm=${app.llm.provider}, kb=${app.cfg.kbUrl}${n ? `, seeded ${n} emails` : ""})`);
  });
}
