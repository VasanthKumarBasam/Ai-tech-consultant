"""Scope Agent: deliverables, exclusions, assumptions, acceptance criteria — then deterministic checks."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from .. import prompts
from ..config import Settings
from ..llm import UsageTracker
from ..models import Deliverable, Feasibility, RequirementsDoc, ReviewIssue, ScopeDoc

VAGUE = re.compile(r"\b(better|improved?|user[- ]friendly|nice|fast(er)?|modern|seamless|intuitive|easy|easier|good)\b", re.I)


class _Deliv(BaseModel):
    title: str
    description: str
    requirement_ids: list[str]
    approach: Literal["configuration", "custom_development", "mixed", "to_be_confirmed"]
    acceptance_criteria: list[str]


class _Scope(BaseModel):
    objective: str
    current_problem: str
    proposed_solution: str
    deliverables: list[_Deliv]
    exclusions: list[str]
    assumptions: list[str]
    dependencies: list[str]
    client_responsibilities: list[str]
    open_questions: list[str]
    risks: list[str]
    timeline: list[str] = []


class _Partial(BaseModel):
    deliverables: list[_Deliv]


def requirements_block(doc: RequirementsDoc, feas: list[Feasibility], only: set[str] | None = None) -> str:
    fmap = {f.requirement_id: f for f in feas}
    lines = []
    for r in doc.requirements:
        if r.status == "removed" or (only is not None and r.id not in only):
            continue
        f = fmap.get(r.id)
        fz = f"{f.classification} ({'verified' if f.verified else 'not verified'}): {f.rationale}" if f else "not checked"
        lines.append(f"<requirement id=\"{r.id}\" status=\"{r.status}\" priority=\"{r.priority}\" origin=\"{r.origin}\">\n"
                     f"  {r.title}: {r.description}\n  acceptance: {' | '.join(r.acceptance_criteria)}\n"
                     f"  open questions: {' | '.join(r.open_questions) or 'none'}\n  feasibility: {fz}\n</requirement>")
    return "\n".join(lines)


def context_block(doc: RequirementsDoc) -> str:
    def notes(label, items):
        return f"<{label}>\n" + "\n".join(f"- {getattr(n, 'kind', '')} {n.text}".replace("-  ", "- ") for n in items) + f"\n</{label}>"
    return "\n".join([
        f"<objective>{doc.project_objective}</objective>", f"<problem>{doc.current_problem}</problem>",
        notes("constraints", doc.constraints), notes("decisions", doc.decisions), notes("open_questions", doc.open_questions),
        notes("commitments", doc.commitments), notes("exclusions", doc.exclusions),
    ])


class ScopeAgent:
    def __init__(self, settings: Settings, llm, tracker: UsageTracker):
        self.s = settings
        self.llm = llm
        self.tracker = tracker

    def _deliverables(self, raw: list[_Deliv], doc: RequirementsDoc, feas: list[Feasibility], start: int,
                      issues: list[ReviewIssue]) -> tuple[list[Deliverable], list[str]]:
        status = {r.id: r.status for r in doc.requirements}
        fmap = {f.requirement_id: f for f in feas}
        out, pending = [], []
        for d in raw:
            keep = [rid for rid in d.requirement_ids if status.get(rid) == "confirmed"]
            dropped = [rid for rid in d.requirement_ids if rid not in keep and rid in status and status[rid] != "removed"]
            if dropped:
                pending.extend(dropped)
                issues.append(ReviewIssue(severity="warn", source="validation",
                                          message=f"Deliverable '{d.title}' referenced unconfirmed {', '.join(dropped)}; moved to pending confirmation."))
            if not keep:
                continue
            approach = d.approach
            classes = {fmap[r].classification for r in keep if r in fmap}
            if "needs_clarification" in classes and approach == "configuration":
                approach = "to_be_confirmed"
                issues.append(ReviewIssue(severity="info", source="validation",
                                          message=f"'{d.title}' set to 'to be confirmed': a linked requirement's platform support is unverified."))
            if approach == "configuration" and "custom_development" in classes:
                approach = "mixed"
            vague = [c for c in d.acceptance_criteria if VAGUE.search(c) and not re.search(r"\d", c)]
            if vague:
                issues.append(ReviewIssue(severity="warn", source="validation",
                                          message=f"'{d.title}' has acceptance criteria that aren't testable: {vague[0][:120]}"))
            out.append(Deliverable(id=f"D-{start + len(out):02d}", title=d.title, description=d.description,
                                   requirement_ids=keep, approach=approach, acceptance_criteria=d.acceptance_criteria))
        return out, pending

    def run(self, doc: RequirementsDoc, feas: list[Feasibility]) -> tuple[ScopeDoc, list[ReviewIssue]]:
        user = f"{context_block(doc)}\n<requirements>\n{requirements_block(doc, feas)}\n</requirements>\nWrite the scope of work."
        ids = {r.id for r in doc.requirements}

        def validate(out: _Scope) -> str | None:
            unknown = sorted({x for d in out.deliverables for x in d.requirement_ids if x not in ids})
            return f"unknown requirement ids {unknown}" if unknown else None

        out = self.llm.structured(stage="scope", model=self.s.model_scope, system=prompts.SCOPE.replace("{company}", self.s.company_name),
                                  user=user, tool_name="record_scope", tool_description="Record the scope of work.", output=_Scope,
                                  tracker=self.tracker, max_tokens=6000, validate=validate)
        issues: list[ReviewIssue] = []
        deliverables, pending = self._deliverables(out.deliverables, doc, feas, 1, issues)
        pending += [r.id for r in doc.requirements if r.status in ("unclear", "disputed")]
        covered = {rid for d in deliverables for rid in d.requirement_ids}
        for r in doc.requirements:
            if r.status == "confirmed" and r.id not in covered:
                issues.append(ReviewIssue(severity="warn", source="validation", message=f"{r.id} ({r.title}) is confirmed but not in any deliverable."))
        scope = ScopeDoc(
            objective=out.objective, current_problem=out.current_problem, proposed_solution=out.proposed_solution,
            deliverables=deliverables, exclusions=out.exclusions, assumptions=out.assumptions, dependencies=out.dependencies,
            client_responsibilities=out.client_responsibilities, open_questions=out.open_questions, risks=out.risks,
            pending_confirmation=sorted(set(pending)), timeline=out.timeline,
        )
        return scope, issues

    def run_partial(self, doc: RequirementsDoc, feas: list[Feasibility], only: set[str], start: int) -> tuple[list[Deliverable], list[ReviewIssue]]:
        """Re-scope only changed/added requirements (used for follow-up meetings)."""
        if not only:
            return [], []
        user = (f"{context_block(doc)}\n<requirements>\n{requirements_block(doc, feas, only)}\n</requirements>\n"
                "Write deliverables ONLY for these requirements (they changed after the baseline was approved).")

        def validate(out: _Partial) -> str | None:
            unknown = sorted({x for d in out.deliverables for x in d.requirement_ids if x not in only})
            return f"only use requirement ids {sorted(only)}; got {unknown}" if unknown else None

        out = self.llm.structured(stage="scope", model=self.s.model_scope, system=prompts.SCOPE.replace("{company}", self.s.company_name),
                                  user=user, tool_name="record_deliverables", tool_description="Record deliverables for the changed requirements.",
                                  output=_Partial, tracker=self.tracker, max_tokens=4000, validate=validate)
        issues: list[ReviewIssue] = []
        deliverables, _ = self._deliverables(out.deliverables, doc, feas, start, issues)
        return deliverables, issues
