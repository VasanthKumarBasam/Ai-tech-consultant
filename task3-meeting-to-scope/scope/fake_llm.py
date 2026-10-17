"""Deterministic stand-in for Claude (LLM_PROVIDER=fake): unit tests and clicking through the UI without a key.
Simple keyword heuristics - good enough to exercise every stage, never presented as model output."""
from __future__ import annotations

import re
from typing import Any

from .llm import LLMError, UsageTracker

WANT = re.compile(r"\b(want|need|must|should|would like|we'd like|can we|could we|add|filter|map|mobile|search|payment|stripe|coupon|email|booth|receipt|quote|language)\b", re.I)
CONSTRAINT = re.compile(r"\b(launch by|deadline|budget|thousand dollars|can't move|by (january|february|march|april|may|june|july|august|september|october|november|december) \d+)", re.I)
REMOVE = re.compile(r"\b(drop|remove|don't want|no longer|out for now)\b", re.I)
NEW = re.compile(r"\b(this is new|new filter|new requirement|we want a new|can we add)\b", re.I)


def _lines(user: str):
    for m in re.finditer(r"^L(\d+) \[[^\]]*\] ([^:]+): (.+)$", user, re.M):
        yield int(m.group(1)), m.group(2).strip(), m.group(3).strip()


def _clients(user: str) -> set[str]:
    m = re.search(r"Client participants: (.*)", user)
    return {x.strip() for x in m.group(1).split(",")} if m else set()


def record_items(user: str) -> dict:
    clients = _clients(user)
    items = []
    for n, spk, text in _lines(user):
        if spk not in clients:
            continue
        kind = "constraint" if CONSTRAINT.search(text) else ("exclusion" if REMOVE.search(text) else ("requirement" if WANT.search(text) else None))
        if not kind:
            continue
        quote = " ".join(text.split()[:18])
        prio = "must" if re.search(r"\bmust\b|main thing|important", text, re.I) else ("could" if re.search(r"nice|optional|maybe", text, re.I) else "unknown")
        items.append({"kind": kind, "text": text[:200], "raised_by": "client", "explicit": True, "priority_hint": prio, "line": n, "quote": quote})
    return {"items": items}


def record_requirements(user: str) -> dict:
    reqs, cons, excl, seen = [], [], [], set()
    for m in re.finditer(r"^(C\d+-\d+) \| (\w+) \| .*? \| L\d+ \| (.+?) \| \"", user, re.M):
        iid, kind, text = m.groups()
        key = text.lower()[:40]
        if key in seen:
            continue
        seen.add(key)
        if kind == "requirement" and len(reqs) < 10:
            title = " ".join(re.sub(r"[^\w\s]", "", text).split()[:5]).capitalize()
            reqs.append({"title": title, "description": text, "status": "unclear" if re.search(r"maybe|not sure|i don't know", text, re.I) else "confirmed",
                         "priority": "must_have" if "must" in text.lower() else "should_have", "origin": "explicit",
                         "acceptance_criteria": [f"{title} works as the client described (to confirm)"], "item_ids": [iid]})
        elif kind == "constraint":
            cons.append({"kind": "deadline" if re.search(r"launch|deadline|by \w+ \d", text, re.I) else "budget", "text": text, "item_ids": [iid]})
        elif kind == "exclusion":
            excl.append({"text": text, "owner": "client", "item_ids": [iid]})
    return {"project_objective": "Improve the client's directory website (fake model summary).", "current_problem": "See requirements.",
            "requirements": reqs, "constraints": cons, "exclusions": excl, "decisions": [], "open_questions": [], "commitments": []}


def record_feasibility(user: str) -> dict:
    out = []
    for m in re.finditer(r'<requirement id="(REQ-\d+)"[^>]*>(.*?)</requirement>', user, re.S):
        rid, body = m.groups()
        st = re.search(r'<kb_lookup status="(\w+)">', body).group(1)
        urls = re.findall(r"<(https?://[^>]+)>", body)
        if st in ("answered", "partial") and urls:
            out.append({"requirement_id": rid, "classification": "existing_configuration", "rationale": "Documented in the help centre (fake model).", "supporting_urls": urls[:1]})
        else:
            out.append({"requirement_id": rid, "classification": "needs_clarification", "rationale": "No documented capability found (fake model).", "supporting_urls": []})
    return {"results": out}


def _req_blocks(user: str):
    return re.findall(r'<requirement id="(REQ-\d+)" status="(\w+)"[^>]*>\n\s*([^:\n]+):', user)


def record_scope(user: str) -> dict:
    delivs = [{"title": title.strip(), "description": f"Deliver: {title.strip()}", "requirement_ids": [rid],
               "approach": "to_be_confirmed" if "needs_clarification" in user.split(rid, 1)[1][:800] else "configuration",
               "acceptance_criteria": [f"{title.strip()} can be demonstrated on staging with 3 example searches"]}
              for rid, st, title in _req_blocks(user) if st == "confirmed"]
    return {"objective": "Deliver the agreed improvements (fake model).", "current_problem": "As described in the meeting.",
            "proposed_solution": "Configure the platform first, custom work only where needed.", "deliverables": delivs,
            "exclusions": ["Visual redesign"], "assumptions": ["Client provides content on time"], "dependencies": [],
            "client_responsibilities": ["Provide data and approvals"], "open_questions": [], "risks": ["Data quality"], "timeline": []}


def record_deliverables(user: str) -> dict:
    return {"deliverables": record_scope(user)["deliverables"]}


def record_estimate(user: str) -> dict:
    tasks = []
    ids = re.findall(r'<deliverable id="(D-\d+)" approach="(\w+)">', user)
    for i, (did, approach) in enumerate(ids):
        base = 3 if approach == "configuration" else 8
        tasks.append({"deliverable_id": did, "name": "Implement", "category": "configuration" if approach == "configuration" else "backend",
                      "low": base * 0.6, "likely": base, "high": base * 1.8, "rationale": "fake model"})
        tasks.append({"deliverable_id": did, "name": "Test", "category": "testing", "low": 1, "likely": 2, "high": 3, "rationale": "fake model"})
        if i == 0 and "project-management task" in user:
            tasks.append({"deliverable_id": did, "name": "Project management", "category": "project_management", "low": 3, "likely": 4, "high": 6, "rationale": "fake model"})
    return {"tasks": tasks}


def record_narrative(user: str) -> dict:
    return {"executive_summary": "[DEMO] We will deliver the agreed improvements, configuring the platform before writing custom code.",
            "client_problem": "[DEMO] Visitors can't find the right provider quickly.", "solution_overview": "[DEMO] See the deliverables below.",
            "next_steps": ["Review this proposal", "Confirm open questions", "Approve to schedule the work"]}


def record_review(user: str) -> dict:
    return {"issues": []}


def record_changes(user: str) -> dict:
    base = dict(re.findall(r'<requirement id="(REQ-\d+)"[^>]*>([^<]+)</requirement>', user))
    changes, sched = [], None
    for m in re.finditer(r"^(C\d+-\d+) \| (\w+) \| .*? \| L\d+ \| (.+?) \| \"", user, re.M):
        iid, kind, text = m.groups()
        words = {w for w in re.findall(r"[a-z]{4,}", text.lower())}
        target = max(base, key=lambda r: len(words & set(re.findall(r"[a-z]{4,}", base[r].lower()))), default=None)
        if re.search(r"launch by|need to launch", text, re.I):
            sched = {"before": "previous date", "after": text[:80], "item_ids": [iid]}
        elif REMOVE.search(text) and target:
            changes.append({"change_type": "removed", "baseline_requirement_id": target, "title": base[target][:60], "item_ids": [iid], "explanation": "client dropped it (fake)"})
        elif NEW.search(text):
            changes.append({"change_type": "added", "title": " ".join(text.split()[:6]), "new_description": text, "item_ids": [iid], "explanation": "new request (fake)"})
    seen, uniq = set(), []
    for c in changes:
        k = (c["change_type"], c.get("baseline_requirement_id") or c["title"])
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return {"changes": uniq, "schedule_change": sched, "other_changes": []}


HANDLERS = {f.__name__: f for f in (record_items, record_requirements, record_feasibility, record_scope, record_deliverables,
                                     record_estimate, record_narrative, record_review, record_changes)}


class FakeLLM:
    provider = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.overrides: dict[str, Any] = {}

    def structured(self, *, stage: str, user: str, tool_name: str, output, tracker: UsageTracker, validate=None, **_: Any):
        self.calls.append(tool_name)
        handler = self.overrides.get(tool_name) or HANDLERS[tool_name]
        parsed = output.model_validate(handler(user))
        err = validate(parsed) if validate else None
        if err:
            raise LLMError(f"{stage}: fake output failed validation: {err}")
        tracker.add(stage, "fake", len(user) // 4, 200, 0.0, 1, 0)
        return parsed
