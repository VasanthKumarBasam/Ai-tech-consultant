"""Knowledge Brain HTTP API (FastAPI). Interactive docs at /docs, contract in shared-docs/kb-api.md."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from .ingestion.pipeline import RunInProgress
from .models import (
    EscalationCreate, EscalationResolve, IngestionRunRequest, QueryRequest, QueryResponse, SearchRequest,
)
from .retrieval.search import retrieve
from .services import Services, build_services

log = logging.getLogger("kb.api")
STATIC = Path(__file__).parent / "static"


def create_app(services: Services | None = None) -> FastAPI:
    svc_holder: dict[str, Services] = {}
    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or build_services()
        svc.pipeline.mark_interrupted()
        svc_holder["svc"] = svc
        scheduler = None
        if svc.settings.scheduler_enabled and svc.settings.daily_schedule_cron:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            from zoneinfo import ZoneInfo

            tz = ZoneInfo(svc.settings.scheduler_timezone)
            scheduler = AsyncIOScheduler(timezone=tz)

            async def daily():
                try:
                    report = await svc.pipeline.run("incremental")
                    log.info("daily ingestion: %s", json.dumps({k: report[k] for k in ("new", "changed", "removed", "failed")}))
                except RunInProgress:
                    log.info("daily ingestion skipped: a run is in progress")

            scheduler.add_job(daily, CronTrigger.from_crontab(svc.settings.daily_schedule_cron, timezone=tz), id="daily_ingest")
            scheduler.start()
            log.info("daily ingestion scheduled: %s (%s)", svc.settings.daily_schedule_cron, svc.settings.scheduler_timezone)
        yield
        if scheduler:
            scheduler.shutdown(wait=False)
        for t in tasks:
            t.cancel()

    app = FastAPI(
        title="Knowledge Brain API",
        version="1.0.0",
        description="Grounded answers with citations over the Brilliant Directories Solution Center, "
        "image-aware retrieval, human escalation, and incremental ingestion.",
        lifespan=lifespan,
    )

    def svc() -> Services:
        return svc_holder["svc"]

    def auth(x_api_key: str | None = Header(None)) -> None:
        key = svc().settings.api_key
        if key and x_api_key != key:
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    @app.middleware("http")
    async def request_id_mw(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    def _err(request: Request, status: int, code: str, message) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {
            "code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}})

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        codes = {401: "unauthorized", 404: "not_found", 409: "conflict", 422: "invalid_request"}
        return _err(request, exc.status_code, codes.get(exc.status_code, "error"), exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_exc(request: Request, exc: RequestValidationError):
        return _err(request, 422, "invalid_request", [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()])

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        return _err(request, 500, "internal_error", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------- routes
    @app.get("/", include_in_schema=False)
    def ui():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health():
        s = svc()
        pages = {r["status"]: r["n"] for r in s.db.query("SELECT status, COUNT(*) n FROM pages GROUP BY status")}
        last = s.db.one("SELECT id,mode,status,started_at,finished_at FROM runs ORDER BY id DESC LIMIT 1")
        return {
            "status": "ok",
            "articles_active": s.db.one("SELECT COUNT(*) n FROM pages WHERE active=1")["n"],
            "pages_by_status": pages,
            **s.store.counts(),
            "embedder": s.store.embedder.name,
            "llm_provider": getattr(s.llm, "provider", "?"),
            "llm_configured": bool(getattr(s.llm, "configured", False)),
            "answer_model": s.settings.answer_model,
            "open_escalations": s.db.one("SELECT COUNT(*) n FROM escalations WHERE status='open'")["n"],
            "last_run": dict(last) if last else None,
            "scheduler": s.settings.daily_schedule_cron if s.settings.scheduler_enabled else None,
        }

    @app.post("/query", response_model=QueryResponse, dependencies=[Depends(auth)])
    async def query(body: QueryRequest, request: Request):
        s = svc()
        return await asyncio.to_thread(
            s.query.answer, body.question, context=body.context, top_k=body.top_k, mode=body.retrieval_mode,
            model=body.model, create_escalation=body.create_escalation, requester=body.requester,
            request_id=request.state.request_id,
        )

    @app.post("/search", dependencies=[Depends(auth)])
    async def search(body: SearchRequest):
        s = svc()
        ev = await asyncio.to_thread(retrieve, s.store, body.query, mode=body.retrieval_mode, top_k=body.top_k)
        return {"results": [e.__dict__ for e in ev]}

    @app.post("/ingestion/run", status_code=202, dependencies=[Depends(auth)])
    async def ingestion_run(body: IngestionRunRequest):
        s = svc()
        from .ingestion.pipeline import _run_lock

        if _run_lock.locked() or s.pipeline.active_run():
            raise HTTPException(409, "an ingestion run is already in progress")

        async def go():
            try:
                await s.pipeline.run(body.mode, limit=body.limit, urls=body.urls)
            except RunInProgress:
                pass

        t = asyncio.create_task(go())
        tasks.add(t)
        t.add_done_callback(tasks.discard)
        await asyncio.sleep(0.05)
        row = s.db.one("SELECT id FROM runs ORDER BY id DESC LIMIT 1")
        return {"accepted": True, "run_id": row["id"] if row else None, "mode": body.mode}

    @app.get("/ingestion/status")
    def ingestion_status():
        s = svc()
        last = s.db.one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        pages = {r["status"]: r["n"] for r in s.db.query("SELECT status, COUNT(*) n FROM pages GROUP BY status")}
        failed = [dict(r) for r in s.db.query(
            "SELECT url, attempts, last_error FROM pages WHERE status='failed' ORDER BY updated_at DESC LIMIT 20")]
        run = dict(last) if last else None
        if run and run.get("counts_json"):
            run["counts"] = json.loads(run.pop("counts_json"))
        return {"running": run is not None and run["status"] == "running", "last_run": run, "pages": pages,
                "recent_failures": failed}

    @app.get("/ingestion/runs")
    def ingestion_runs(limit: int = 20):
        rows = svc().db.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["counts"] = json.loads(d.pop("counts_json") or "{}")
            d["counts"].pop("failures", None)
            out.append(d)
        return {"runs": out}

    @app.post("/ingestion/runs/{run_id}/rollback", dependencies=[Depends(auth)])
    def rollback(run_id: int):
        try:
            return svc().pipeline.rollback(run_id)
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.get("/articles/{article_id}")
    def article(article_id: str):
        row = svc().db.one(
            "SELECT * FROM article_versions WHERE article_id=? AND is_active=1 ORDER BY id DESC LIMIT 1", (article_id,))
        if not row:
            raise HTTPException(404, "article not found")
        d = dict(row)
        d["sections"] = json.loads(d.pop("sections_json"))
        d["images"] = json.loads(d.pop("images_json"))
        return d

    @app.get("/escalations")
    def list_escalations(status: str | None = None):
        items = svc().escalations.list(status)
        for i in items:
            i["evidence"] = json.loads(i.pop("evidence_json") or "[]")
        return {"escalations": items}

    @app.post("/escalations", status_code=201, dependencies=[Depends(auth)])
    def create_escalation(body: EscalationCreate):
        return svc().escalations.create(body.question, body.reason, body.context, requester=body.requester)

    @app.post("/escalations/{eid}/resolve", dependencies=[Depends(auth)])
    def resolve_escalation(eid: int, body: EscalationResolve):
        try:
            return svc().escalations.resolve(eid, body.answer, body.resolved_by, body.scope, body.expires_at,
                                             body.supporting_url, body.add_to_knowledge)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/escalations/{eid}/dismiss", dependencies=[Depends(auth)])
    def dismiss_escalation(eid: int, resolved_by: str = "reviewer"):
        if not svc().escalations.get(eid):
            raise HTTPException(404, "escalation not found")
        return svc().escalations.dismiss(eid, resolved_by)

    @app.get("/stats")
    def stats():
        r = svc().db.one(
            """SELECT COUNT(*) n, AVG(latency_ms) lat, SUM(cost_usd) cost, SUM(status='answered') answered,
                      SUM(status='insufficient_evidence') insufficient, SUM(status='partial') partial FROM query_log""")
        return dict(r)

    return app


app = create_app()
