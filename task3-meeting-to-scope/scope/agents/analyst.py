"""Transcript Analyst: staged extraction (map over chunks) + consolidation (reduce) with verified source links."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field

from .. import prompts
from ..config import Settings
from ..llm import UsageTracker
from ..models import Constraint, ExtractedItem, Note, Requirement, RequirementsDoc, SourceRef
from ..transcripts import Transcript, chunk_lines, client_speakers, verify_quote


class _Item(BaseModel):
    kind: Literal["requirement", "decision", "constraint", "open_question", "commitment", "exclusion"]
    text: str = Field(description="One self-contained statement in plain words.")
    raised_by: Literal["client", "consultant", "unclear"]
    explicit: bool
    priority_hint: Literal["must", "should", "could", "unknown"] = "unknown"
    line: int = Field(description="Line number (the number after L) where the quote appears.")
    quote: str = Field(description="Exact words copied from that line, max ~25 words.")


class _Extraction(BaseModel):
    items: list[_Item]


class _Req(BaseModel):
    title: str = Field(description="Short name, e.g. 'Search filters'.")
    description: str
    status: Literal["confirmed", "unclear", "disputed"]
    priority: Literal["must_have", "should_have", "optional"]
    origin: Literal["explicit", "inferred"]
    acceptance_criteria: list[str]
    dependencies: list[str] = []
    open_questions: list[str] = []
    item_ids: list[str] = Field(description="Extracted item ids that support this requirement.")


class _Constraint(BaseModel):
    kind: Literal["deadline", "budget", "technical", "content", "approval", "other"]
    text: str
    item_ids: list[str]


class _Note(BaseModel):
    text: str
    owner: Literal["client", "us", "unknown"] = "unknown"
    item_ids: list[str]


class _Merged(BaseModel):
    project_objective: str
    current_problem: str
    requirements: list[_Req]
    constraints: list[_Constraint] = []
    decisions: list[_Note] = []
    open_questions: list[_Note] = []
    commitments: list[_Note] = []
    exclusions: list[_Note] = []


class TranscriptAnalyst:
    def __init__(self, settings: Settings, llm, tracker: UsageTracker):
        self.s = settings
        self.llm = llm
        self.tracker = tracker

    # ---------------------------------------------------------------- map
    def extract(self, t: Transcript) -> list[ExtractedItem]:
        chunks = chunk_lines(t, self.s.chunk_max_chars, self.s.chunk_overlap_lines)
        header = (f"Meeting: {t.title}\nDate: {t.date or 'unknown'}\nParticipants: {t.meta.get('participants', 'unknown')}\n"
                  f"Client participants: {', '.join(sorted(client_speakers(t))) or 'unknown'}")

        def run(idx_chunk):
            idx, chunk = idx_chunk
            lo, hi = chunk[0].n, chunk[-1].n
            user = (f"<meeting>\n{header}\n</meeting>\n<section index=\"{idx + 1}\" of=\"{len(chunks)}\" lines=\"L{lo}-L{hi}\">\n"
                    f"{t.render(chunk)}\n</section>\nExtract the items from this section.")
            valid = {l.n for l in chunk}

            def validate(out: _Extraction) -> str | None:
                bad = sorted({i.line for i in out.items if i.line not in valid})
                return f"line numbers {bad} are not in this section (L{lo}-L{hi})" if bad else None

            out = self.llm.structured(
                stage="extract", model=self.s.model_extract, system=prompts.EXTRACT.replace("{company}", self.s.company_name),
                user=user, tool_name="record_items", tool_description="Record the items extracted from this transcript section.",
                output=_Extraction, tracker=self.tracker, max_tokens=6000, validate=validate,
            )
            return idx, out

        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as pool:
            results = sorted(pool.map(run, enumerate(chunks)), key=lambda r: r[0])
        clients = client_speakers(t)
        items: list[ExtractedItem] = []
        for idx, out in results:
            for n, it in enumerate(out.items, 1):
                ref = verify_quote(t, it.line, it.quote)
                raised = it.raised_by
                if ref.verified and ref.speaker and clients:  # trust the transcript over the model for who said it
                    raised = "client" if ref.speaker in clients else ("consultant" if raised == "client" and not it.explicit else raised)
                items.append(ExtractedItem(id=f"C{idx + 1}-{n}", kind=it.kind, text=it.text, raised_by=raised, explicit=it.explicit,
                                           priority_hint=it.priority_hint, source=ref))
        return items

    # ------------------------------------------------------------- reduce
    def consolidate(self, t: Transcript, items: list[ExtractedItem], id_offset: int = 0) -> RequirementsDoc:
        by_id = {i.id: i for i in items}
        listing = "\n".join(
            f'{i.id} | {i.kind} | raised_by={i.raised_by} explicit={i.explicit} priority={i.priority_hint} | L{i.source.line} | {i.text} | "{i.source.quote}"'
            for i in items
        )
        user = f"<meeting>\n{t.title} ({t.date or 'date unknown'})\n</meeting>\n<items>\n{listing}\n</items>\nConsolidate these into the requirements record."

        def validate(out: _Merged) -> str | None:
            unknown = sorted({x for r in out.requirements for x in r.item_ids if x not in by_id}
                             | {x for group in (out.constraints, out.decisions, out.open_questions, out.commitments, out.exclusions)
                                for n in group for x in n.item_ids if x not in by_id})
            if unknown:
                return f"unknown item ids: {unknown[:10]}"
            empty = [r.title for r in out.requirements if not r.item_ids]
            return f"requirements without supporting item_ids: {empty}" if empty else None

        out = self.llm.structured(
            stage="merge", model=self.s.model_merge, system=prompts.MERGE.replace("{company}", self.s.company_name), user=user,
            tool_name="record_requirements", tool_description="Record the consolidated requirements.", output=_Merged,
            tracker=self.tracker, max_tokens=8000, validate=validate,
        )

        def refs(ids: list[str]) -> list[SourceRef]:
            seen, out_refs = set(), []
            for x in ids:
                src = by_id[x].source
                key = (src.meeting_id, src.line)
                if key not in seen:
                    seen.add(key)
                    out_refs.append(src)
            return sorted(out_refs, key=lambda r: r.line)

        reqs = []
        for n, r in enumerate(out.requirements, 1):
            sup = [by_id[x] for x in r.item_ids]
            origin = r.origin
            if all(i.raised_by == "consultant" and not i.explicit for i in sup):
                origin = "inferred"  # nobody on the client side asked for it
            reqs.append(Requirement(
                id=f"REQ-{n + id_offset:03d}", title=r.title, description=r.description, status=r.status, priority=r.priority,
                origin=origin, acceptance_criteria=r.acceptance_criteria, dependencies=r.dependencies,
                open_questions=r.open_questions, sources=refs(r.item_ids), added_in_meeting=t.meeting_id,
            ))
        note = lambda n: Note(text=n.text, owner=n.owner, sources=refs(n.item_ids))  # noqa: E731
        return RequirementsDoc(
            project_objective=out.project_objective, current_problem=out.current_problem, requirements=reqs,
            constraints=[Constraint(kind=c.kind, text=c.text, sources=refs(c.item_ids)) for c in out.constraints],
            decisions=[note(n) for n in out.decisions], open_questions=[note(n) for n in out.open_questions],
            commitments=[note(n) for n in out.commitments], exclusions=[note(n) for n in out.exclusions],
        )
