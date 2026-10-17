"""Runs the agent team for a meeting and stores the result as a new draft version.

Initial meeting:   extract -> merge -> feasibility -> scope -> estimate -> proposal -> review
Follow-up meeting: extract -> change (vs APPROVED baseline) -> feasibility (changed only)
                   -> scope (changed only) -> estimate (changed only) -> proposal -> review
"""
from __future__ import annotations

import logging
import traceback
from typing import Callable

from .agents.analyst import TranscriptAnalyst
from .agents.change import ChangeAnalyst
from .agents.estimator import EstimationAgent, compute_estimate
from .agents.feasibility import FeasibilityAgent
from .agents.proposal import ProposalAgent
from .agents.scope_writer import ScopeAgent
from .config import Settings
from .kb_client import KnowledgeClient
from .llm import LLMError, UsageTracker
from .models import (
    ChangeItem, ChangeReport, Deliverable, EstimateTask, Feasibility, ReviewIssue, ScopeDoc, ScopePackage,
)
from .prompts import PROMPT_VERSIONS
from .store import Store
from .transcripts import parse_transcript

log = logging.getLogger("scope.orchestrator")

BASELINE_STAGES = ["extract", "merge", "feasibility", "scope", "estimate", "proposal", "review"]
CHANGE_STAGES = ["extract", "change", "feasibility", "scope", "estimate", "proposal", "review"]


class PipelineError(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, settings: Settings, store: Store, llm, kb: KnowledgeClient):
        self.s = settings
        self.store = store
        self.llm = llm
        self.kb = kb

    # --------------------------------------------------------------- public
    def start(self, meeting_id: str) -> int:
        m = self.store.meeting(meeting_id)
        if not m:
            raise PipelineError("meeting not found")
        if m["kind"] == "follow_up" and not self.store.latest_approved(m["project_id"]):
            raise PipelineError("This project has no approved scope yet. Approve the baseline proposal first — "
                                "follow-up changes are always compared with the approved version, never with a draft.")
        return self.store.create_job(m["project_id"], meeting_id, CHANGE_STAGES if m["kind"] == "follow_up" else BASELINE_STAGES)

    def run_job(self, job_id: int) -> int | None:
        job = self.store.job(job_id)
        m = self.store.meeting(job["meeting_id"])
        try:
            fn = self._followup if m["kind"] == "follow_up" else self._baseline
            vid = fn(job_id, m)
            self.store.finish_job(job_id, "completed", version_id=vid)
            return vid
        except Exception as e:  # recorded on the job and shown in the UI
            log.error("job %s failed: %s\n%s", job_id, e, traceback.format_exc())
            current = self.store.job(job_id).get("stage")
            if current:
                self.store.job_stage(job_id, current, "failed", str(e)[:300])
            self.store.finish_job(job_id, "failed", error=f"{type(e).__name__}: {e}"[:1000])
            return None

    # -------------------------------------------------------------- helpers
    def _stage(self, job_id: int, name: str) -> Callable[[str | None], None]:
        self.store.job_stage(job_id, name, "running")
        return lambda detail=None: self.store.job_stage(job_id, name, "done", detail)

    def _proposal_and_review(self, job_id, doc, scope, feas, est, change, transcript_text, tracker):
        proposal = ProposalAgent(self.s, self.llm, tracker)
        done = self._stage(job_id, "proposal")
        narrative = proposal.narrative(doc, scope, feas, est, change)
        done()
        done = self._stage(job_id, "review")
        constraints_text = " ".join(c.text for c in doc.constraints) + " " + transcript_text
        issues = proposal.consistency(narrative, doc, scope, feas, constraints_text)
        try:
            issues += proposal.review(narrative, doc, scope, feas, est, change)
        except LLMError as e:
            issues.append(ReviewIssue(severity="warn", source="model_review", message=f"Automated review failed ({e}); review manually."))
        done(f"{sum(i.severity == 'block' for i in issues)} blocking, {sum(i.severity == 'warn' for i in issues)} warnings")
        return narrative, issues

    # -------------------------------------------------------------- baseline
    def _baseline(self, job_id: int, m: dict) -> int:
        tracker = UsageTracker()
        t = parse_transcript(m["id"], m["transcript"])
        if not t.lines:
            raise PipelineError("the transcript has no readable lines")
        analyst = TranscriptAnalyst(self.s, self.llm, tracker)
        done = self._stage(job_id, "extract")
        items = analyst.extract(t)
        done(f"{len(items)} items from {len(t.lines)} lines")
        done = self._stage(job_id, "merge")
        doc = analyst.consolidate(t, items)
        done(f"{len(doc.requirements)} requirements")
        done = self._stage(job_id, "feasibility")
        fa = FeasibilityAgent(self.s, self.llm, self.kb, tracker)
        feas = fa.run(doc.requirements)
        done(f"{sum(f.verified for f in feas)}/{len(feas)} verified against the Knowledge Brain")
        done = self._stage(job_id, "scope")
        scope, issues = ScopeAgent(self.s, self.llm, tracker).run(doc, feas)
        done(f"{len(scope.deliverables)} deliverables")
        done = self._stage(job_id, "estimate")
        est, est_issues = EstimationAgent(self.s, self.llm, tracker).run(scope.deliverables, doc, feas)
        issues += est_issues
        done(f"{est.total_with_contingency:g} h expected")
        narrative, review = self._proposal_and_review(job_id, doc, scope, feas, est, None, m["transcript"], tracker)
        pkg = ScopePackage(project_id=m["project_id"], kind="baseline", meeting_ids=[m["id"]], requirements=doc, feasibility=feas,
                           scope=scope, estimate=est, narrative=narrative, review_issues=issues + review, extracted_items=items,
                           usage=tracker.usage, models=self.s.models(), prompt_versions=PROMPT_VERSIONS)
        pkg.usage.by_stage["knowledge_brain"] = {"calls": len(fa.kb_calls), "cost_usd": round(sum(c.get("cost_usd", 0) or 0 for c in fa.kb_calls), 6)}
        return self.store.add_version(m["project_id"], "baseline", pkg.model_dump(mode="json"), None, m["id"])["id"]

    # -------------------------------------------------------------- follow-up
    def _followup(self, job_id: int, m: dict) -> int:
        base_v = self.store.latest_approved(m["project_id"])
        if not base_v:
            raise PipelineError("no approved baseline to compare with")
        base = ScopePackage.model_validate(base_v["payload"])
        tracker = UsageTracker()
        t = parse_transcript(m["id"], m["transcript"])
        analyst = TranscriptAnalyst(self.s, self.llm, tracker)
        done = self._stage(job_id, "extract")
        items = analyst.extract(t)
        done(f"{len(items)} items")

        done = self._stage(job_id, "change")
        ca = ChangeAnalyst(self.s, self.llm, tracker)
        out = ca.detect(base.requirements, items)
        doc, changes = ChangeAnalyst.apply(base.requirements, out, items, m["id"])
        changed = {rid for rid, (ct, _) in changes.items() if ct in ("modified", "added")}
        removed = {rid for rid, (ct, _) in changes.items() if ct == "removed"}
        done(f"{len(changed)} added/modified, {len(removed)} removed")

        done = self._stage(job_id, "feasibility")
        fa = FeasibilityAgent(self.s, self.llm, self.kb, tracker)
        new_feas = fa.run([r for r in doc.requirements if r.id in changed])
        feas: list[Feasibility] = [f for f in base.feasibility if f.requirement_id not in changed | removed] + new_feas
        done(f"re-checked {len(new_feas)} requirement(s)")

        # deliverables touching a changed/removed requirement are re-scoped; the rest are kept verbatim with their tasks
        done = self._stage(job_id, "scope")
        touched = changed | removed
        affected = [d for d in base.scope.deliverables if set(d.requirement_ids) & touched]
        kept = [d for d in base.scope.deliverables if d not in affected]
        status = {r.id: r.status for r in doc.requirements}
        rescope = ({rid for d in affected for rid in d.requirement_ids} | changed) - removed
        rescope = {rid for rid in rescope if status.get(rid) not in (None, "removed")}
        next_d = max([int(d.id.split("-")[1]) for d in base.scope.deliverables] + [0]) + 1
        new_delivs, issues = ScopeAgent(self.s, self.llm, tracker).run_partial(doc, feas, rescope, next_d)
        deliverables = kept + new_delivs
        removed_titles = [f"{rid} {next(r.title for r in doc.requirements if r.id == rid)}" for rid in sorted(removed)]
        scope = ScopeDoc(**{**base.scope.model_dump(), "deliverables": deliverables})
        scope.exclusions = scope.exclusions + [f"Removed at the client's request in {m['title']} ({m['meeting_date']}): {title}" for title in removed_titles]
        scope.pending_confirmation = sorted({r.id for r in doc.requirements if r.status in ("unclear", "disputed")})
        if out.schedule_change:
            scope.timeline = [f"Go-live moved: {out.schedule_change.after} (was {out.schedule_change.before})"] + \
                [x for x in scope.timeline if "go-live" not in x.lower() and "launch" not in x.lower()]
        scope.open_questions = scope.open_questions + [q.text for q in doc.open_questions if q.text.startswith("Conflict")]
        done(f"kept {len(kept)}, re-scoped {len(affected)}, new {len(new_delivs)}")

        done = self._stage(job_id, "estimate")
        kept_ids = {d.id for d in kept}
        kept_tasks = [t_ for t_ in base.estimate.tasks if t_.deliverable_id in kept_ids]
        next_t = max([int(t_.id.split("-")[1]) for t_ in base.estimate.tasks] + [0]) + 1
        new_tasks, est_issues = EstimationAgent(self.s, self.llm, tracker).tasks_for(new_delivs, doc, feas, start=next_t, include_pm=False)
        if not any(t_.category == "project_management" for t_ in kept_tasks):  # PM task lived on a re-scoped deliverable
            pm = [t_ for t_ in base.estimate.tasks if t_.category == "project_management"]
            if pm and deliverables:
                kept_tasks += [EstimateTask(**{**pm[0].model_dump(), "deliverable_id": deliverables[0].id})]
        est = compute_estimate(kept_tasks + new_tasks, deliverables, feas, self.s,
                               notes=[f"Revision of version {base_v['number']}: unchanged deliverables keep their approved estimates."])
        done(f"{est.total_with_contingency:g} h expected (was {base.estimate.total_with_contingency:g} h)")

        report = self._change_report(base, base_v, doc, changes, out, deliverables, est, m)
        narrative, review = self._proposal_and_review(job_id, doc, scope, feas, est, report, m["transcript"], tracker)
        pkg = ScopePackage(project_id=m["project_id"], kind="revision", meeting_ids=base.meeting_ids + [m["id"]], requirements=doc,
                           feasibility=feas, scope=scope, estimate=est, narrative=narrative, review_issues=issues + est_issues + review,
                           change_report=report, extracted_items=items, usage=tracker.usage, models=self.s.models(),
                           prompt_versions=PROMPT_VERSIONS)
        pkg.usage.by_stage["knowledge_brain"] = {"calls": len(fa.kb_calls), "cost_usd": round(sum(c.get("cost_usd", 0) or 0 for c in fa.kb_calls), 6)}
        return self.store.add_version(m["project_id"], "revision", pkg.model_dump(mode="json"), base_v["id"], m["id"])["id"]

    @staticmethod
    def _attributed_hours(deliverables: list[Deliverable], per: dict, rid: str) -> float:
        """Hours of the deliverables containing a requirement, split evenly across their requirements (approximate)."""
        total = 0.0
        for d in deliverables:
            if rid in d.requirement_ids and d.id in per:
                total += per[d.id]["expected"] / max(1, len(d.requirement_ids))
        return round(total, 1)

    def _change_report(self, base: ScopePackage, base_v: dict, doc, changes, out, deliverables, est, m) -> ChangeReport:
        before_reqs = {r.id: r for r in base.requirements.requirements}
        after_reqs = {r.id: r for r in doc.requirements}
        items = []
        for rid, (ct, c) in changes.items():
            b, a = before_reqs.get(rid), after_reqs.get(rid)
            items.append(ChangeItem(
                change_type=ct, requirement_id=rid, title=(a or b).title if (a or b) else c.title,
                before=b.description if b else None, after=a.description if a and ct != "removed" else None,
                explanation=c.explanation, sources=a.sources[-3:] if a else [], out_of_scope=(ct == "added"),
                hours_before=self._attributed_hours(base.scope.deliverables, base.estimate.per_deliverable, rid) if b else 0,
                hours_after=self._attributed_hours(deliverables, est.per_deliverable, rid) if a and ct != "removed" else 0,
            ))
        sched = None
        if out.schedule_change:
            sc = out.schedule_change
            sched = {"before": sc.before, "after": sc.after}
        added = [i.title for i in items if i.change_type == "added"]
        summary = (f"{len([i for i in items if i.change_type == 'added'])} added, {len([i for i in items if i.change_type == 'modified'])} modified, "
                   f"{len([i for i in items if i.change_type == 'removed'])} removed"
                   + (f"; out of original scope (needs separate approval): {', '.join(added)}" if added else "")
                   + (f"; schedule moved from {sched['before']} to {sched['after']}" if sched else "")
                   + f". Expected effort {base.estimate.total_with_contingency:g} h -> {est.total_with_contingency:g} h.")
        return ChangeReport(baseline_version=base_v["number"], baseline_meeting=base.meeting_ids[-1], followup_meeting=m["id"], items=items,
                            schedule_change=sched, other_changes=[o.text for o in out.other_changes],
                            hours_before=base.estimate.total_with_contingency, hours_after=est.total_with_contingency, summary=summary)
