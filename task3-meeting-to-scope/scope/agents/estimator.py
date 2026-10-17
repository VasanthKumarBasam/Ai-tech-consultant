"""Estimation Agent: the model breaks deliverables into tasks with low/likely/high hours;
all arithmetic (totals, PERT, contingency, price) is done here in code, never by the model."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .. import prompts
from ..config import Settings
from ..llm import UsageTracker
from ..models import Deliverable, Estimate, EstimateTask, Feasibility, RequirementsDoc, ReviewIssue

Category = Literal["discovery", "configuration", "design", "frontend", "backend", "integration", "data", "testing", "deployment", "project_management"]


class _Task(BaseModel):
    deliverable_id: str
    name: str
    category: Category
    low: float
    likely: float
    high: float
    rationale: str
    assumptions: list[str] = []
    confidence: Literal["low", "medium", "high"] = "medium"


class _Out(BaseModel):
    tasks: list[_Task]


def r1(x: float) -> float:
    return round(x + 1e-9, 1)


def compute_estimate(tasks: list[EstimateTask], deliverables: list[Deliverable], feas: list[Feasibility], s: Settings,
                     notes: list[str] | None = None) -> Estimate:
    """Pure function: totals, PERT, contingency and indicative price from the task list."""
    fmap = {f.requirement_id: f for f in feas}
    uncertain = [d for d in deliverables if d.approach == "to_be_confirmed"
                 or any(fmap.get(r) and fmap[r].classification == "needs_clarification" for r in d.requirement_ids)]
    low = sum(t.low for t in tasks)
    likely = sum(t.likely for t in tasks)
    high = sum(t.high for t in tasks)
    pert = sum((t.low + 4 * t.likely + t.high) / 6 for t in tasks)
    pct = min(s.max_contingency_pct, s.base_contingency_pct + s.contingency_per_unclear_pct * len(uncertain))
    cont = pert * pct / 100
    per = {}
    for d in deliverables:
        ts = [t for t in tasks if t.deliverable_id == d.id]
        per[d.id] = {"title": d.title, "low": r1(sum(t.low for t in ts)), "likely": r1(sum(t.likely for t in ts)),
                     "high": r1(sum(t.high for t in ts)), "expected": r1(sum((t.low + 4 * t.likely + t.high) / 6 for t in ts))}
    total = pert + cont
    return Estimate(
        tasks=tasks, total_low=r1(low), total_likely=r1(likely), total_high=r1(high), total_expected=r1(pert),
        contingency_pct=round(pct, 1), contingency_hours=r1(cont), total_with_contingency=r1(total), hourly_rate=s.hourly_rate,
        currency=s.currency, price_expected=round(total * s.hourly_rate), price_low=round(low * s.hourly_rate),
        price_high=round(high * (1 + pct / 100) * s.hourly_rate),
        notes=(notes or []) + [f"Contingency {pct:.0f}%: base {s.base_contingency_pct:.0f}% + {s.contingency_per_unclear_pct:.0f}% for each of "
                               f"{len(uncertain)} deliverable(s) with unconfirmed requirements or platform support."],
        per_deliverable=per,
    )


class EstimationAgent:
    def __init__(self, settings: Settings, llm, tracker: UsageTracker):
        self.s = settings
        self.llm = llm
        self.tracker = tracker

    def tasks_for(self, deliverables: list[Deliverable], doc: RequirementsDoc, feas: list[Feasibility], start: int = 1,
                  include_pm: bool = True) -> tuple[list[EstimateTask], list[ReviewIssue]]:
        if not deliverables:
            return [], []
        reqs = {r.id: r for r in doc.requirements}
        fmap = {f.requirement_id: f for f in feas}
        blocks = []
        for d in deliverables:
            rq = "\n".join(
                f"    {rid}: {reqs[rid].title}: {reqs[rid].description} [feasibility: "
                f"{fmap[rid].classification + ' - ' + fmap[rid].rationale if rid in fmap else 'not checked'}]"
                for rid in d.requirement_ids if rid in reqs)
            blocks.append(f"<deliverable id=\"{d.id}\" approach=\"{d.approach}\">\n  {d.title}: {d.description}\n"
                          f"  acceptance: {' | '.join(d.acceptance_criteria)}\n  requirements:\n{rq}\n</deliverable>")
        constraints = "\n".join(f"- {c.kind}: {c.text}" for c in doc.constraints) or "- none recorded"
        user = (f"<constraints>\n{constraints}\n</constraints>\n<deliverables>\n" + "\n".join(blocks) + "\n</deliverables>\n"
                + ("Include one project-management task for the whole project on the first deliverable.\n" if include_pm else
                   "These deliverables are a change to an approved project; do not add general project-management overhead.\n")
                + "Estimate every deliverable.")
        ids = {d.id for d in deliverables}

        def validate(out: _Out) -> str | None:
            unknown = sorted({t.deliverable_id for t in out.tasks} - ids)
            missing = sorted(ids - {t.deliverable_id for t in out.tasks})
            if unknown or missing:
                return f"tasks must cover exactly deliverables {sorted(ids)} (missing {missing}, unknown {unknown})"
            neg = [t.name for t in out.tasks if min(t.low, t.likely, t.high) <= 0]
            return f"hours must be positive: {neg}" if neg else None

        out = self.llm.structured(stage="estimate", model=self.s.model_estimate, system=prompts.ESTIMATE.replace("{company}", self.s.company_name),
                                  user=user, tool_name="record_estimate", tool_description="Record the task breakdown with hour estimates.",
                                  output=_Out, tracker=self.tracker, max_tokens=6000, validate=validate)
        issues: list[ReviewIssue] = []
        tasks: list[EstimateTask] = []
        for t in out.tasks:
            lo, li, hi = sorted([t.low, t.likely, t.high])
            if (lo, li, hi) != (t.low, t.likely, t.high):
                issues.append(ReviewIssue(severity="info", source="validation", message=f"Reordered hours for '{t.name}' so low <= likely <= high."))
            if li > self.s.max_task_hours:
                issues.append(ReviewIssue(severity="warn", source="validation",
                                          message=f"'{t.name}' is {li:g} likely hours; tasks over {self.s.max_task_hours:g}h should be split before approval."))
            tasks.append(EstimateTask(id=f"T-{start + len(tasks):02d}", deliverable_id=t.deliverable_id, name=t.name, category=t.category,
                                      low=lo, likely=li, high=hi, rationale=t.rationale, assumptions=t.assumptions, confidence=t.confidence))
        for d in deliverables:
            ts = [t for t in tasks if t.deliverable_id == d.id]
            if d.approach == "configuration" and sum(t.likely for t in ts) > 24:
                issues.append(ReviewIssue(severity="warn", source="validation",
                                          message=f"'{d.title}' is configuration-only but estimated at {sum(t.likely for t in ts):g} likely hours; check the breakdown."))
            if not any(t.category == "testing" for t in ts):
                issues.append(ReviewIssue(severity="warn", source="validation", message=f"'{d.title}' has no testing task."))
        return tasks, issues

    def run(self, deliverables: list[Deliverable], doc: RequirementsDoc, feas: list[Feasibility]) -> tuple[Estimate, list[ReviewIssue]]:
        tasks, issues = self.tasks_for(deliverables, doc, feas)
        return compute_estimate(tasks, deliverables, feas, self.s), issues
