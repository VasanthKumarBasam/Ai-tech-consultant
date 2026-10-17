"""Client-ready Markdown for a version: proposal, scope of work, estimate, and change report."""
from __future__ import annotations

from .models import ScopePackage

FEAS_LABEL = {
    "existing_configuration": "Platform setting / add-on",
    "existing_with_limitation": "Platform feature, with limits",
    "custom_development": "Custom development",
    "needs_clarification": "To be confirmed",
}
APPROACH = {"configuration": "Configuration", "custom_development": "Custom development", "mixed": "Configuration + custom", "to_be_confirmed": "To be confirmed"}
PRIORITY = {"must_have": "Must have", "should_have": "Should have", "optional": "Optional"}


def money(x: float, cur: str) -> str:
    return f"{cur} {x:,.0f}"


def render_markdown(pkg: ScopePackage, meta: dict, company: str = "Northstar Web Studio") -> str:
    s, e, n = pkg.scope, pkg.estimate, pkg.narrative
    req = {r.id: r for r in pkg.requirements.requirements}
    feas = {f.requirement_id: f for f in pkg.feasibility}
    status = meta.get("status", "draft")
    out: list[str] = []
    w = out.append
    w(f"# Proposal: {meta.get('project_name', 'Project')}")
    w(f"Prepared by {company} for {meta.get('client', 'the client')}. Version {meta.get('number', '?')} ({pkg.kind}), status: **{status}**.")
    if status != "approved" and status != "sent":
        w("\n> DRAFT - not approved for sending. Figures are indicative until a reviewer approves this version.")
    w("\n## Summary\n" + n.executive_summary)
    w("\n## The problem\n" + n.client_problem)
    w("\n## Our approach\n" + n.solution_overview)

    if pkg.change_report:
        cr = pkg.change_report
        w(f"\n## What changed since version {cr.baseline_version}\n{cr.summary}\n")
        w("| Change | Requirement | Before | After | Hours (approx.) |\n| --- | --- | --- | --- | --- |")
        for i in cr.items:
            flag = " **(outside the approved scope: separate approval needed)**" if i.out_of_scope else ""
            w(f"| {i.change_type.title()} | {i.requirement_id} {i.title}{flag} | {(i.before or '-')[:160]} | {(i.after or '-')[:160]} | {i.hours_before:g} -> {i.hours_after:g} |")
        if cr.schedule_change:
            w(f"\n**Schedule:** {cr.schedule_change['before']} -> {cr.schedule_change['after']}")
        for o in cr.other_changes:
            w(f"- {o}")

    w("\n## Scope of work\n### Objective\n" + s.objective)
    w("\n### Deliverables")
    for d in s.deliverables:
        w(f"\n**{d.id} {d.title}** ({APPROACH[d.approach]})  \n{d.description}  \nCovers: {', '.join(f'{r} {req[r].title}' for r in d.requirement_ids if r in req)}  \nAccepted when:")
        for c in d.acceptance_criteria:
            w(f"- {c}")
    if s.pending_confirmation:
        w("\n### Waiting for your confirmation (not included in the estimate)")
        for rid in s.pending_confirmation:
            if rid in req:
                w(f"- {rid} {req[rid].title}: {req[rid].description}")
    w("\n### Platform check")
    w("We checked each requirement against the Brilliant Directories documentation before estimating, so you only pay for custom work where it's needed.\n")
    w("| Requirement | Result | Why | Source |\n| --- | --- | --- | --- |")
    for r in pkg.requirements.requirements:
        if r.status == "removed" or r.id not in feas:
            continue
        f = feas[r.id]
        src = ", ".join(f"[{x.title}]({x.url})" for x in f.kb_sources[:2]) or "-"
        w(f"| {r.id} {r.title} | {FEAS_LABEL[f.classification]}{'' if f.verified or f.classification == 'custom_development' else ' (unverified)'} | {f.rationale} | {src} |")

    w("\n## Estimate")
    w("| Task | Deliverable | Low | Likely | High |\n| --- | --- | ---: | ---: | ---: |")
    for t in e.tasks:
        w(f"| {t.id} {t.name} | {t.deliverable_id} | {t.low:g} | {t.likely:g} | {t.high:g} |")
    w(f"| **Total** | | **{e.total_low:g}** | **{e.total_likely:g}** | **{e.total_high:g}** |")
    w(f"\nExpected effort (weighted): **{e.total_expected:g} hours**, plus {e.contingency_pct:g}% contingency ({e.contingency_hours:g} h) = "
      f"**{e.total_with_contingency:g} hours**.")
    w(f"\nIndicative price at {money(e.hourly_rate, e.currency)}/hour: **{money(e.price_expected, e.currency)}** "
      f"(range {money(e.price_low, e.currency)} to {money(e.price_high, e.currency)}). Taxes not included. "
      "This becomes a quote only once this version is approved and sent.")
    for note in e.notes:
        w(f"- {note}")

    if s.timeline:
        w("\n## Timeline")
        for x in s.timeline:
            w(f"- {x}")
    for title, items in (("Your responsibilities", s.client_responsibilities), ("Assumptions", s.assumptions),
                         ("Dependencies", s.dependencies), ("Not included", s.exclusions), ("Risks", s.risks),
                         ("Open questions", s.open_questions)):
        if items:
            w(f"\n## {title}")
            for x in items:
                w(f"- {x}")
    w("\n## Next steps")
    for i, x in enumerate(n.next_steps, 1):
        w(f"{i}. {x}")
    w("\n---")
    if meta.get("approved_by"):
        w(f"Approved by {meta['approved_by']} on {meta.get('approved_at')} UTC.")
    else:
        w("Not yet approved.")
    return "\n".join(out) + "\n"
