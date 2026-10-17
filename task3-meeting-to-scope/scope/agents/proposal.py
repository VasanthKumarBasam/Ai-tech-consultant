"""Proposal & Review Agent: the model writes the narrative only; numbers come from structured data.
A deterministic consistency check plus a model review produce the issues shown to the approver."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from .. import prompts
from ..config import Settings
from ..llm import UsageTracker
from ..models import ChangeReport, Estimate, Feasibility, ProposalNarrative, RequirementsDoc, ReviewIssue, ScopeDoc

MONEY_OR_HOURS = re.compile(r"(\$\s?\d[\d,]*|\b\d[\d,.]*\s?(hours|hrs|h)\b|\b\d[\d,]*\s?(usd|dollars)\b)", re.I)
DATE = re.compile(r"\b(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|aug(ust)?|sep(tember)?|oct(ober)?|nov(ember)?|dec(ember)?)\.?\s+\d{1,2}(st|nd|rd|th)?\b", re.I)


class _Issue(BaseModel):
    severity: Literal["block", "warn", "info"]
    message: str


class _Review(BaseModel):
    issues: list[_Issue]


def summary_block(doc: RequirementsDoc, scope: ScopeDoc, feas: list[Feasibility], est: Estimate, change: ChangeReport | None) -> str:
    fmap = {f.requirement_id: f for f in feas}
    reqs = "\n".join(f"- {r.id} {r.title} [{r.status}, {r.priority}] feasibility={fmap[r.id].classification if r.id in fmap else 'n/a'}"
                     for r in doc.requirements if r.status != "removed")
    dels = "\n".join(f"- {d.id} {d.title} ({d.approach}): {d.description} [reqs {', '.join(d.requirement_ids)}]" for d in scope.deliverables)
    ch = ""
    if change:
        ch = "\n<changes_since_approved_baseline>\n" + "\n".join(f"- {i.change_type}: {i.title}" + (" (out of original scope)" if i.out_of_scope else "")
                                                                for i in change.items) + (f"\n- schedule: {change.schedule_change}" if change.schedule_change else "") + "\n</changes_since_approved_baseline>"
    return (f"<objective>{scope.objective}</objective>\n<problem>{scope.current_problem}</problem>\n<solution>{scope.proposed_solution}</solution>\n"
            f"<requirements>\n{reqs}\n</requirements>\n<deliverables>\n{dels}\n</deliverables>\n"
            f"<exclusions>{'; '.join(scope.exclusions)}</exclusions>\n<pending_confirmation>{', '.join(scope.pending_confirmation) or 'none'}</pending_confirmation>\n"
            f"<risks>{'; '.join(scope.risks)}</risks>\n<estimate>expected {est.total_with_contingency:g} hours incl. {est.contingency_pct:g}% contingency</estimate>{ch}")


class ProposalAgent:
    def __init__(self, settings: Settings, llm, tracker: UsageTracker):
        self.s = settings
        self.llm = llm
        self.tracker = tracker

    def narrative(self, doc, scope, feas, est, change=None) -> ProposalNarrative:
        user = summary_block(doc, scope, feas, est, change) + "\nWrite the narrative sections."
        return self.llm.structured(stage="proposal", model=self.s.model_proposal, system=prompts.PROPOSAL.replace("{company}", self.s.company_name),
                                   user=user, tool_name="record_narrative", tool_description="Record the proposal narrative.",
                                   output=ProposalNarrative, tracker=self.tracker, max_tokens=2500)

    def consistency(self, narrative: ProposalNarrative, doc: RequirementsDoc, scope: ScopeDoc, feas: list[Feasibility],
                    constraints_text: str) -> list[ReviewIssue]:
        issues: list[ReviewIssue] = []
        text = " ".join([narrative.executive_summary, narrative.client_problem, narrative.solution_overview, *narrative.next_steps])
        for m in MONEY_OR_HOURS.finditer(text):
            issues.append(ReviewIssue(severity="block", message=f"Narrative states a number ('{m.group(0)}'); prices and hours must come from the estimate table only."))
        known_dates = {d.group(0).lower() for d in DATE.finditer(constraints_text)}
        for d in DATE.finditer(text):
            if d.group(0).lower() not in known_dates:
                issues.append(ReviewIssue(severity="warn", message=f"Narrative mentions a date not stated by the client: '{d.group(0)}'."))
        if feas and all(f.kb_status == "unavailable" for f in feas):
            issues.append(ReviewIssue(severity="block", message="Platform feasibility was not checked (Knowledge Brain unavailable). Re-run before approval."))
        for f in feas:
            if f.classification.startswith("existing") and not f.verified:
                issues.append(ReviewIssue(severity="block", message=f"{f.requirement_id} is marked as an existing platform feature without verified evidence."))
        unclear = [f.requirement_id for f in feas if f.classification == "needs_clarification"]
        if unclear:
            issues.append(ReviewIssue(severity="info", message=f"Platform support still to be confirmed for {', '.join(unclear)}; flagged in the proposal."))
        if scope.pending_confirmation:
            issues.append(ReviewIssue(severity="info", message=f"Requirements awaiting client confirmation (not priced as deliverables): {', '.join(scope.pending_confirmation)}."))
        unverified = [s for r in doc.requirements for s in r.sources if not s.verified]
        if unverified:
            issues.append(ReviewIssue(severity="warn", message=f"{len(unverified)} transcript quote(s) could not be matched to the cited line; check them in the Requirements tab."))
        return issues

    def review(self, narrative: ProposalNarrative, doc, scope, feas, est, change=None) -> list[ReviewIssue]:
        user = (summary_block(doc, scope, feas, est, change) + "\n<proposal_narrative>\n" + narrative.model_dump_json(indent=1)
                + "\n</proposal_narrative>\nReview the narrative against the structured data.")
        out = self.llm.structured(stage="review", model=self.s.model_review, system=prompts.REVIEW.replace("{company}", self.s.company_name),
                                  user=user, tool_name="record_review", tool_description="Record review issues (empty if none).",
                                  output=_Review, tracker=self.tracker, max_tokens=1500)
        return [ReviewIssue(severity=i.severity, message=i.message, source="model_review") for i in out.issues]
