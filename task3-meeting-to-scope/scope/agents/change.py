"""Change Analyst: compares a follow-up meeting with the APPROVED baseline (never with an unapproved draft)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .. import prompts
from ..config import Settings
from ..llm import UsageTracker
from ..models import Constraint, ExtractedItem, Note, Requirement, RequirementsDoc, SourceRef


class _Change(BaseModel):
    change_type: Literal["added", "removed", "modified", "conflict"]
    baseline_requirement_id: str | None = None
    title: str
    new_description: str | None = None
    new_priority: Literal["must_have", "should_have", "optional"] | None = None
    new_status: Literal["confirmed", "unclear", "disputed"] | None = None
    new_acceptance_criteria: list[str] = []
    item_ids: list[str]
    explanation: str


class _Schedule(BaseModel):
    before: str
    after: str
    item_ids: list[str]


class _Other(BaseModel):
    text: str
    item_ids: list[str]


class _Out(BaseModel):
    changes: list[_Change]
    schedule_change: _Schedule | None = None
    other_changes: list[_Other] = []


class ChangeAnalyst:
    def __init__(self, settings: Settings, llm, tracker: UsageTracker):
        self.s = settings
        self.llm = llm
        self.tracker = tracker

    def detect(self, baseline: RequirementsDoc, items: list[ExtractedItem]) -> _Out:
        by_id = {i.id: i for i in items}
        base_ids = {r.id for r in baseline.requirements if r.status != "removed"}
        reqs = "\n".join(f"<requirement id=\"{r.id}\" status=\"{r.status}\" priority=\"{r.priority}\">{r.title}: {r.description}</requirement>"
                         for r in baseline.requirements if r.status != "removed")
        cons = "\n".join(f"- {c.kind}: {c.text}" for c in baseline.constraints)
        listing = "\n".join(f'{i.id} | {i.kind} | raised_by={i.raised_by} | L{i.source.line} | {i.text} | "{i.source.quote}"' for i in items)
        user = (f"<approved_baseline>\n{reqs}\n<constraints>\n{cons}\n</constraints>\n</approved_baseline>\n"
                f"<followup_items>\n{listing}\n</followup_items>\nList the changes.")

        def validate(out: _Out) -> str | None:
            for c in out.changes:
                if c.change_type in ("removed", "modified") and c.baseline_requirement_id not in base_ids:
                    return f"'{c.title}': {c.change_type} needs a baseline_requirement_id from {sorted(base_ids)}"
                if c.change_type == "added" and c.baseline_requirement_id:
                    return f"'{c.title}': added changes must not reuse a baseline id"
                if not c.item_ids or any(x not in by_id for x in c.item_ids):
                    return f"'{c.title}': cite valid follow-up item_ids"
                if c.change_type in ("added", "modified") and not c.new_description:
                    return f"'{c.title}': give the full new_description"
            return None

        return self.llm.structured(stage="change", model=self.s.model_change, system=prompts.CHANGE.replace("{company}", self.s.company_name),
                                   user=user, tool_name="record_changes", tool_description="Record changes relative to the approved baseline.",
                                   output=_Out, tracker=self.tracker, max_tokens=4000, validate=validate)

    @staticmethod
    def apply(baseline: RequirementsDoc, out: _Out, items: list[ExtractedItem], meeting_id: str) -> tuple[RequirementsDoc, dict]:
        """Build the revised requirements deterministically. Returns (revised doc, {req_id: (change_type, change)})."""
        by_id = {i.id: i for i in items}
        refs = lambda ids: [by_id[x].source for x in ids if x in by_id]  # noqa: E731
        revised = baseline.model_copy(deep=True)
        reqs = {r.id: r for r in revised.requirements}
        next_n = max([int(r.id.split("-")[1]) for r in revised.requirements] + [0]) + 1
        changes: dict[str, tuple[str, _Change]] = {}
        for c in out.changes:
            if c.change_type == "removed":
                r = reqs[c.baseline_requirement_id]
                r.status = "removed"
                r.sources = r.sources + refs(c.item_ids)
                changes[r.id] = ("removed", c)
            elif c.change_type == "modified":
                r = reqs[c.baseline_requirement_id]
                r.description = c.new_description or r.description
                r.priority = c.new_priority or r.priority
                r.status = c.new_status or ("confirmed" if r.status == "unclear" else r.status)
                if c.new_acceptance_criteria:
                    r.acceptance_criteria = c.new_acceptance_criteria
                r.sources = r.sources + refs(c.item_ids)
                changes[r.id] = ("modified", c)
            elif c.change_type == "added":
                rid = f"REQ-{next_n:03d}"
                next_n += 1
                revised.requirements.append(Requirement(
                    id=rid, title=c.title, description=c.new_description or c.title, status=c.new_status or "confirmed",
                    priority=c.new_priority or "should_have", origin="explicit",
                    acceptance_criteria=c.new_acceptance_criteria or [f"{c.title} works as described (to confirm)"],
                    sources=refs(c.item_ids), added_in_meeting=meeting_id, out_of_scope_change=True,
                ))
                changes[rid] = ("added", c)
            else:  # conflict
                key = c.baseline_requirement_id or f"conflict-{len(changes) + 1}"
                changes[key] = ("conflict", c)
                revised.open_questions.append(_note(f"Conflict to resolve: {c.title}. {c.explanation}", refs(c.item_ids)))
        if out.schedule_change:
            sc = out.schedule_change
            revised.constraints = [k for k in revised.constraints if k.kind != "deadline"] + [
                Constraint(kind="deadline", text=f"{sc.after} (changed from {sc.before})", sources=refs(sc.item_ids))]
        for o in out.other_changes:
            revised.decisions.append(_note(o.text, refs(o.item_ids)))
        return revised, changes


def _note(text: str, sources: list[SourceRef]) -> Note:
    return Note(text=text, owner="unknown", sources=sources)
