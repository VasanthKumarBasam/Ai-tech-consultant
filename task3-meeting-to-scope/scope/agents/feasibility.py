"""Product Feasibility Agent: asks the Knowledge Brain (Task 1 API) about every requirement, then classifies."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel

from .. import prompts
from ..config import Settings
from ..kb_client import KnowledgeClient, KnowledgeUnavailable
from ..llm import UsageTracker
from ..models import Feasibility, KBSource, Requirement


class _Class(BaseModel):
    requirement_id: str
    classification: Literal["existing_configuration", "existing_with_limitation", "custom_development", "needs_clarification"]
    rationale: str
    supporting_urls: list[str] = []


class _Out(BaseModel):
    results: list[_Class]


def kb_question(r: Requirement) -> str:
    return (f"Does the Brilliant Directories platform support the following, and if so which setting, feature or add-on "
            f"provides it and what are its limitations? {r.title}: {r.description}")[:1800]


class FeasibilityAgent:
    def __init__(self, settings: Settings, llm, kb: KnowledgeClient, tracker: UsageTracker):
        self.s = settings
        self.llm = llm
        self.kb = kb
        self.tracker = tracker
        self.kb_calls: list[dict] = []

    def _lookup(self, r: Requirement) -> dict:
        q = kb_question(r)
        try:
            res = self.kb.query(q, context=f"Client requirement {r.id} for a directory website")
            cited = [s for s in res.get("sources", []) if s.get("cited")]
            out = {"question": q, "status": res["status"], "answer": res.get("answer", ""),
                   "sources": [{"title": s["title"], "url": s["url"]} for s in cited],
                   "escalation_id": res.get("escalation_id"), "cost_usd": (res.get("usage") or {}).get("cost_usd", 0.0),
                   "latency_ms": res.get("latency_ms", 0)}
        except (KnowledgeUnavailable, ValueError) as e:
            out = {"question": q, "status": "unavailable", "answer": "", "sources": [], "escalation_id": None, "error": str(e)}
        self.kb_calls.append(out)
        return out

    def run(self, requirements: list[Requirement]) -> list[Feasibility]:
        active = [r for r in requirements if r.status != "removed"]
        if not active:
            return []
        with ThreadPoolExecutor(max_workers=max(1, self.s.kb_concurrency)) as pool:
            lookups = dict(zip([r.id for r in active], pool.map(self._lookup, active)))
        if all(l["status"] == "unavailable" for l in lookups.values()):
            return [Feasibility(requirement_id=r.id, classification="needs_clarification",
                                rationale="Knowledge Brain unavailable, so platform capabilities were not checked. Re-run before approving.",
                                kb_question=lookups[r.id]["question"], kb_status="unavailable", verified=False) for r in active]

        blocks = []
        for r in active:
            l = lookups[r.id]
            srcs = "\n".join(f"    - {s['title']} <{s['url']}>" for s in l["sources"]) or "    (none)"
            blocks.append(f"<requirement id=\"{r.id}\" status=\"{r.status}\">\n  {r.title}: {r.description}\n"
                          f"  <kb_lookup status=\"{l['status']}\">\n  {l['answer'][:1500]}\n  sources:\n{srcs}\n  </kb_lookup>\n</requirement>")
        user = "\n".join(blocks) + "\nClassify every requirement."
        ids = {r.id for r in active}

        def validate(out: _Out) -> str | None:
            got = {x.requirement_id for x in out.results}
            if got != ids:
                return f"classify exactly these requirement ids: {sorted(ids)} (missing {sorted(ids - got)}, unknown {sorted(got - ids)})"
            for x in out.results:
                l = lookups[x.requirement_id]
                if x.classification.startswith("existing"):
                    if l["status"] not in ("answered", "partial"):
                        return f"{x.requirement_id}: '{x.classification}' needs an answered/partial lookup; it was {l['status']}"
                    if not set(x.supporting_urls) & {s["url"] for s in l["sources"]}:
                        return f"{x.requirement_id}: list the supporting source URLs from its lookup"
            return None

        out = self.llm.structured(
            stage="feasibility", model=self.s.model_feasibility, system=prompts.FEASIBILITY.replace("{company}", self.s.company_name),
            user=user, tool_name="record_feasibility", tool_description="Record the feasibility classification of each requirement.",
            output=_Out, tracker=self.tracker, max_tokens=4000, validate=validate,
        )
        result = []
        for x in sorted(out.results, key=lambda x: x.requirement_id):
            l = lookups[x.requirement_id]
            urls = set(x.supporting_urls)
            used = [KBSource(**s) for s in l["sources"] if s["url"] in urls] or [KBSource(**s) for s in l["sources"]]
            result.append(Feasibility(
                requirement_id=x.requirement_id, classification=x.classification, rationale=x.rationale, kb_question=l["question"],
                kb_status=l["status"], kb_answer=l["answer"][:2000], kb_sources=used, kb_escalation_id=l.get("escalation_id"),
                verified=l["status"] in ("answered", "partial") and bool(l["sources"]),
            ))
        return result
