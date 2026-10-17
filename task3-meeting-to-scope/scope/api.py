"""Meeting-to-Scope HTTP API + Scope Builder UI (FastAPI)."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .agents.estimator import compute_estimate
from .config import TASK_DIR, Settings
from .kb_client import KnowledgeClient
from .llm import build_llm
from .models import EstimateTask, ScopePackage
from .orchestrator import Orchestrator, PipelineError
from .render import render_markdown
from .store import ApprovalError, Store, slug

STATIC = Path(__file__).parent / "static"
SAMPLES = TASK_DIR / "transcripts"


@dataclass
class Services:
    settings: Settings
    store: Store
    orchestrator: Orchestrator
    kb: KnowledgeClient
    llm: object


def build_services(settings: Settings | None = None, llm=None, kb: KnowledgeClient | None = None) -> Services:
    s = settings or Settings()
    s.ensure_dirs()
    store = Store(s.db_path)
    kb = kb or KnowledgeClient(s.kb_url, s.kb_api_key, s.kb_timeout_s)
    llm = llm or build_llm(s)
    return Services(s, store, Orchestrator(s, store, llm, kb), kb, llm)


class ProjectIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    client: str = Field(..., min_length=2, max_length=120)


class MeetingIn(BaseModel):
    title: str = Field(..., min_length=2, max_length=200)
    date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    kind: Literal["initial", "follow_up"]
    transcript: str = Field(..., min_length=50, max_length=400_000)


class ApproveIn(BaseModel):
    reviewer: str = Field(..., min_length=1, max_length=100)
    note: str | None = Field(None, max_length=1000)
    acknowledge_blocks: bool = False


class SendIn(BaseModel):
    sender: str = Field(..., min_length=1, max_length=100)


class TaskEdit(BaseModel):
    low: float = Field(..., gt=0, le=500)
    likely: float = Field(..., gt=0, le=500)
    high: float = Field(..., gt=0, le=500)
    editor: str = Field(..., min_length=1, max_length=100)
    reason: str | None = Field(None, max_length=300)


def meta_for(svc: Services, v: dict) -> dict:
    p = svc.store.project(v["project_id"]) or {}
    return {**{k: v[k] for k in ("number", "status", "approved_by", "approved_at")}, "project_name": p.get("name"), "client": p.get("client")}


def create_app(services: Services | None = None) -> FastAPI:
    app = FastAPI(title="Meeting to Scope API", version="1.0.0",
                  description="Turns client meeting transcripts into requirements, feasibility checks (via the Knowledge Brain), "
                              "scope, estimates and proposals with change tracking and enforced human approval.")
    svc = services or build_services()

    def err(status: int, code: str, message) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})

    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: HTTPException):
        return err(e.status_code, {404: "not_found", 409: "conflict", 422: "invalid_request"}.get(e.status_code, "error"), e.detail)

    @app.exception_handler(RequestValidationError)
    async def _val(_: Request, e: RequestValidationError):
        return err(422, "invalid_request", "; ".join(f"{'.'.join(map(str, x['loc'][1:]))}: {x['msg']}" for x in e.errors()))

    @app.exception_handler(ApprovalError)
    async def _appr(_: Request, e: ApprovalError):
        return err(e.status, "approval_required" if e.status != 404 else "not_found", str(e))

    @app.exception_handler(PipelineError)
    async def _pipe(_: Request, e: PipelineError):
        return err(409, "pipeline_blocked", str(e))

    @app.get("/", include_in_schema=False)
    def ui():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health():
        kb = svc.kb.health()
        return {"status": "ok", "llm_provider": getattr(svc.llm, "provider", "?"), "models": svc.settings.models(),
                "kb": {"url": svc.settings.kb_url, "ok": kb.get("ok", False), "articles": kb.get("articles_active")},
                "rate": {"hourly": svc.settings.hourly_rate, "currency": svc.settings.currency}}

    @app.get("/projects")
    def projects():
        return {"projects": svc.store.projects()}

    @app.post("/projects", status_code=201)
    def create_project(body: ProjectIn):
        return svc.store.create_project(body.name, body.client)

    @app.get("/projects/{pid}")
    def project(pid: int):
        p = svc.store.project(pid)
        if not p:
            raise HTTPException(404, "project not found")
        approved = svc.store.latest_approved(pid)
        return {"project": p, "meetings": svc.store.meetings(pid), "versions": svc.store.versions(pid), "jobs": svc.store.jobs(pid),
                "approved_version": approved["id"] if approved else None}

    @app.post("/projects/{pid}/meetings", status_code=201)
    def add_meeting(pid: int, body: MeetingIn):
        if not svc.store.project(pid):
            raise HTTPException(404, "project not found")
        return svc.store.add_meeting(pid, body.title, body.date, body.kind, body.transcript)

    @app.get("/meetings/{mid}")
    def meeting(mid: str):
        m = svc.store.meeting(mid)
        if not m:
            raise HTTPException(404, "meeting not found")
        return m

    @app.post("/meetings/{mid}/analyse", status_code=202)
    def analyse(mid: str):
        job_id = svc.orchestrator.start(mid)
        threading.Thread(target=svc.orchestrator.run_job, args=(job_id,), daemon=True).start()
        return svc.store.job(job_id)

    @app.get("/jobs/{jid}")
    def job(jid: int):
        j = svc.store.job(jid)
        if not j:
            raise HTTPException(404, "job not found")
        return j

    @app.get("/versions/{vid}")
    def version(vid: int):
        v = svc.store.version(vid)
        if not v:
            raise HTTPException(404, "version not found")
        v["markdown"] = render_markdown(ScopePackage.model_validate(v["payload"]), meta_for(svc, v), svc.settings.company_name)
        return v

    @app.get("/versions/{vid}/markdown", response_class=PlainTextResponse)
    def version_md(vid: int):
        v = svc.store.version(vid)
        if not v:
            raise HTTPException(404, "version not found")
        return render_markdown(ScopePackage.model_validate(v["payload"]), meta_for(svc, v), svc.settings.company_name)

    @app.patch("/versions/{vid}/tasks/{task_id}")
    def edit_task(vid: int, task_id: str, body: TaskEdit):
        v = svc.store.version(vid)
        if not v:
            raise HTTPException(404, "version not found")
        pkg = ScopePackage.model_validate(v["payload"])
        lo, li, hi = sorted([body.low, body.likely, body.high])
        tasks: list[EstimateTask] = []
        found = None
        for t in pkg.estimate.tasks:
            if t.id == task_id:
                found = t
                t = t.model_copy(update={"low": lo, "likely": li, "high": hi,
                                         "rationale": f"{t.rationale} [adjusted by {body.editor}{': ' + body.reason if body.reason else ''}]"})
            tasks.append(t)
        if not found:
            raise HTTPException(404, "task not found")
        pkg.estimate = compute_estimate(tasks, pkg.scope.deliverables, pkg.feasibility, svc.settings, notes=[n for n in pkg.estimate.notes if not n.startswith("Contingency")])
        out = svc.store.update_payload(vid, pkg.model_dump(mode="json"), body.editor,
                                       f"{task_id}: {found.low:g}/{found.likely:g}/{found.high:g} -> {lo:g}/{li:g}/{hi:g}")
        out["markdown"] = render_markdown(ScopePackage.model_validate(out["payload"]), meta_for(svc, out), svc.settings.company_name)
        return out

    @app.post("/versions/{vid}/approve")
    def approve(vid: int, body: ApproveIn):
        return svc.store.approve(vid, body.reviewer, body.note, body.acknowledge_blocks)

    @app.post("/versions/{vid}/send")
    def send(vid: int, body: SendIn):
        v = svc.store.assert_sendable(vid)  # 409 unless approved and unchanged since approval
        p = svc.store.project(v["project_id"])
        md = render_markdown(ScopePackage.model_validate(v["payload"]), meta_for(svc, v), svc.settings.company_name)
        path = svc.settings.sent_dir / f"{slug(p['name'])}-v{v['number']}.md"
        path.write_text(md)
        return svc.store.mark_sent(vid, body.sender, str(path))

    @app.get("/audit")
    def audit():
        return {"entries": svc.store.audit()}

    @app.get("/samples")
    def samples():
        return {"samples": sorted(p.name for p in SAMPLES.glob("*.txt"))}

    @app.post("/demo/load", status_code=201)
    def demo_load():
        """Creates the two sample projects with their role-played meetings (idempotent)."""
        names = {p["name"] for p in svc.store.projects()}
        created = []
        plan = [("Bayside Home Services Directory", "Bayside Home Services",
                 [("meeting_a_bayside_discovery.txt", "Discovery: search and mobile", "2026-10-05", "initial", "meeting-a"),
                  ("meeting_c_bayside_followup.txt", "Follow-up: proposal review and changes", "2026-10-14", "follow_up", "meeting-c")]),
                ("Midwest Artisan Guild Directory", "Midwest Artisan Guild",
                 [("meeting_b_artisan_guild_checkout.txt", "Checkout, notifications and payments", "2026-10-08", "initial", "meeting-b")])]
        for name, client, meetings in plan:
            if name in names:
                continue
            p = svc.store.create_project(name, client)
            for fname, title, date, kind, mid in meetings:
                svc.store.add_meeting(p["id"], title, date, kind, (SAMPLES / fname).read_text(), meeting_id=mid)
            created.append(p)
        return {"created": created, "projects": svc.store.projects()}

    return app


def _lazy_app() -> FastAPI:
    return create_app()

