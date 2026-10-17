"""Structured records passed between agents. Every stage has a typed input and output."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Priority = Literal["must_have", "should_have", "optional"]
ReqStatus = Literal["confirmed", "unclear", "disputed", "removed"]
FeasClass = Literal["existing_configuration", "existing_with_limitation", "custom_development", "needs_clarification"]


class TranscriptLine(BaseModel):
    n: int                       # 1-based line number in the transcript file
    timestamp: str | None
    speaker: str | None
    text: str


class SourceRef(BaseModel):
    meeting_id: str
    line: int
    timestamp: str | None = None
    speaker: str | None = None
    quote: str
    verified: bool = True        # quote found in the transcript at (or near) that line


class ExtractedItem(BaseModel):
    """One fact pulled from one transcript chunk (stage 1, map step)."""
    id: str                      # C{chunk}-{n}, assigned by code
    kind: Literal["requirement", "decision", "constraint", "open_question", "commitment", "exclusion"]
    text: str
    raised_by: Literal["client", "consultant", "unclear"]
    explicit: bool               # the client actually asked for it (vs. suggested/inferred)
    priority_hint: Literal["must", "should", "could", "unknown"] = "unknown"
    source: SourceRef


class Requirement(BaseModel):
    id: str                      # REQ-001 ...
    title: str
    description: str
    status: ReqStatus
    priority: Priority
    origin: Literal["explicit", "inferred"]
    acceptance_criteria: list[str] = []
    dependencies: list[str] = []
    open_questions: list[str] = []
    sources: list[SourceRef] = []
    added_in_meeting: str | None = None
    out_of_scope_change: bool = False   # added after the approved baseline -> needs separate quote


class Constraint(BaseModel):
    kind: Literal["deadline", "budget", "technical", "content", "approval", "other"]
    text: str
    sources: list[SourceRef] = []


class Note(BaseModel):
    text: str
    owner: Literal["client", "us", "unknown"] = "unknown"
    sources: list[SourceRef] = []


class RequirementsDoc(BaseModel):
    project_objective: str
    current_problem: str
    requirements: list[Requirement]
    constraints: list[Constraint] = []
    decisions: list[Note] = []
    open_questions: list[Note] = []
    commitments: list[Note] = []
    exclusions: list[Note] = []


class KBSource(BaseModel):
    title: str
    url: str


class Feasibility(BaseModel):
    requirement_id: str
    classification: FeasClass
    rationale: str
    kb_question: str
    kb_status: str               # answered | partial | insufficient_evidence | error | unavailable
    kb_answer: str = ""
    kb_sources: list[KBSource] = []
    kb_escalation_id: int | None = None
    verified: bool               # True only when backed by an answered/partial KB lookup with sources


class Deliverable(BaseModel):
    id: str                      # D-01 ...
    title: str
    description: str
    requirement_ids: list[str]
    approach: Literal["configuration", "custom_development", "mixed", "to_be_confirmed"]
    acceptance_criteria: list[str]


class ScopeDoc(BaseModel):
    objective: str
    current_problem: str
    proposed_solution: str
    deliverables: list[Deliverable]
    exclusions: list[str]
    assumptions: list[str]
    dependencies: list[str]
    client_responsibilities: list[str]
    open_questions: list[str]
    risks: list[str]
    pending_confirmation: list[str] = []    # requirement ids not yet confirmed (not in deliverables)
    timeline: list[str] = []                # milestones derived from constraints


class EstimateTask(BaseModel):
    id: str                      # T-01 ...
    deliverable_id: str
    name: str
    category: Literal["discovery", "configuration", "design", "frontend", "backend", "integration", "data", "testing", "deployment", "project_management"]
    low: float
    likely: float
    high: float
    rationale: str
    assumptions: list[str] = []
    confidence: Literal["low", "medium", "high"] = "medium"


class Estimate(BaseModel):
    tasks: list[EstimateTask]
    total_low: float
    total_likely: float
    total_high: float
    total_expected: float        # PERT (low + 4*likely + high) / 6
    contingency_pct: float
    contingency_hours: float
    total_with_contingency: float
    hourly_rate: float
    currency: str
    price_expected: float
    price_low: float
    price_high: float
    notes: list[str] = []
    per_deliverable: dict[str, dict] = {}


class ReviewIssue(BaseModel):
    severity: Literal["block", "warn", "info"]
    message: str
    source: Literal["consistency_check", "model_review", "validation"] = "consistency_check"


class ProposalNarrative(BaseModel):
    executive_summary: str
    client_problem: str
    solution_overview: str
    next_steps: list[str]


class ChangeItem(BaseModel):
    change_type: Literal["added", "removed", "modified", "unchanged", "conflict"]
    requirement_id: str
    title: str
    before: str | None = None
    after: str | None = None
    explanation: str
    sources: list[SourceRef] = []
    out_of_scope: bool = False
    hours_before: float = 0
    hours_after: float = 0


class ChangeReport(BaseModel):
    baseline_version: int
    baseline_meeting: str
    followup_meeting: str
    items: list[ChangeItem]
    schedule_change: dict | None = None     # {"before": "...", "after": "...", "sources": [...]}
    other_changes: list[str] = []
    hours_before: float
    hours_after: float
    summary: str


class Usage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    retries: int = 0
    by_stage: dict[str, dict] = Field(default_factory=dict)


class ScopePackage(BaseModel):
    """Everything one pipeline run produces; stored as a version."""
    project_id: int
    kind: Literal["baseline", "revision"]
    meeting_ids: list[str]
    requirements: RequirementsDoc
    feasibility: list[Feasibility]
    scope: ScopeDoc
    estimate: Estimate
    narrative: ProposalNarrative
    review_issues: list[ReviewIssue]
    change_report: ChangeReport | None = None
    extracted_items: list[ExtractedItem] = []
    usage: Usage
    models: dict[str, str]
    prompt_versions: dict[str, str]
