"""Meeting-to-Scope evaluation against hand-written gold annotations.

    python evaluation/run_eval.py                                      # configured models
    python evaluation/run_eval.py --models claude-sonnet-5-5,claude-opus-5-5,claude-haiku-5-5

For each model configuration it runs the full pipeline in a throwaway database:
Meeting A (baseline) -> approve (by the harness) -> Meeting C (follow-up changes) -> Meeting B,
then scores ~50 cases from evaluation/gold/*.json plus the approval gates.
Needs the Knowledge Brain running (KB_URL) and ANTHROPIC_API_KEY (or LLM_PROVIDER=fake for a dry run).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from scope.agents.scope_writer import VAGUE  # noqa: E402
from scope.api import build_services, create_app  # noqa: E402
from scope.config import Settings  # noqa: E402

GOLD = Path(__file__).parent / "gold"
RESULTS = Path(__file__).parent / "results"


def low(*parts) -> str:
    return " ".join(str(p or "") for p in parts).lower()


def has_all(text: str, groups: list[list[str]]) -> bool:
    return all(any(t.lower() in text for t in g) for g in groups)


def field_text(pkg: dict, field: str) -> str:
    doc, scope = pkg["requirements"], pkg["scope"]
    if field in ("constraints", "exclusions", "decisions", "commitments"):
        return low(*[x["text"] for x in doc[field]])
    if field == "open_questions":
        return low(*[x["text"] for x in doc["open_questions"]], *[q for r in doc["requirements"] for q in r["open_questions"]])
    if field == "scope_exclusions":
        return low(*scope["exclusions"])
    if field == "scope_open_questions":
        return low(*scope["open_questions"], *scope.get("pending_confirmation", []))
    return low(*scope.get(field, []))


def match_req(pkg: dict, g: dict) -> dict | None:
    for r in pkg["requirements"]["requirements"]:
        if r["status"] != "removed" and has_all(low(r["title"], r["description"]), g["match"]):
            return r
    return None


def score_baseline(pkg: dict, gold: dict, meeting: str) -> list[dict]:
    rows = []
    feas = {f["requirement_id"]: f for f in pkg["feasibility"]}
    add = lambda cid, kind, title, ok, detail="": rows.append({"meeting": meeting, "case": cid, "kind": kind, "title": title, "passed": int(bool(ok)), "detail": detail[:300]})  # noqa: E731
    for g in gold["requirements"]:
        r = match_req(pkg, g)
        if g.get("not_confirmed_must"):
            anywhere = has_all(low(field_text(pkg, "open_questions"), field_text(pkg, "exclusions"), field_text(pkg, "scope_exclusions"),
                                   field_text(pkg, "scope_open_questions"), field_text(pkg, "decisions")), g["match"])
            ok = (r is not None and not (r["status"] == "confirmed" and r["priority"] == "must_have")) or (r is None and anywhere)
            add(g["id"], "requirement", g["title"], ok, f"{r['id']} {r['status']}/{r['priority']}" if r else ("only in notes" if anywhere else "not captured"))
            continue
        ok, why = r is not None, []
        if r:
            if g.get("status") and r["status"] not in g["status"]:
                ok = False
                why.append(f"status {r['status']}")
            if g.get("priority") and r["priority"] not in g["priority"]:
                ok = False
                why.append(f"priority {r['priority']}")
            if g.get("text_all") and not has_all(low(r["title"], r["description"]), g["text_all"]):
                ok = False
                why.append("missing details")
        add(g["id"], "requirement", g["title"], ok, f"{r['id']}: {'; '.join(why) or 'ok'}" if r else "not found")
        if g.get("feasibility"):
            f = feas.get(r["id"]) if r else None
            add(f"{g['id']}-F", "feasibility", f"Platform check: {g['title']}", f is not None and f["classification"] in g["feasibility"],
                f"{f['classification']} (verified={f['verified']})" if f else "no requirement/feasibility")
    for c in gold["checks"]:
        if c.get("type") == "rating_undecided":
            confirmed = [r for r in pkg["requirements"]["requirements"] if r["status"] == "confirmed" and "rating" in low(r["description"])]
            hedged = all(re.search(r"undecided|not confirmed|to be confirmed|tbd|pending|decide|open question|excluded|not included", low(r["description"])) for r in confirmed)
            ok = hedged or "rating" in field_text(pkg, "open_questions") and not confirmed
            add(c["id"], "check", c["title"], ok or hedged, f"{len(confirmed)} confirmed requirement(s) mention rating")
        elif c.get("type") == "not_in_deliverables":
            hit = [d["id"] for d in pkg["scope"]["deliverables"] if any(t in low(d["title"], d["description"], *d["acceptance_criteria"]) for t in c["terms"])]
            add(c["id"], "check", c["title"], not hit, f"found in {hit}" if hit else "")
        else:
            text = " ".join(field_text(pkg, f) for f in c["fields"])
            add(c["id"], "check", c["title"], has_all(text, c["all"]), "")
    est = pkg["estimate"]
    lo, hi = gold["reference_estimate"]["range"]
    add(f"{meeting}-EST", "estimate", f"Likely hours within reference band {lo}-{hi}", lo <= est["total_likely"] <= hi,
        f"likely {est['total_likely']} h, expected incl. contingency {est['total_with_contingency']} h")
    refs = [s for r in pkg["requirements"]["requirements"] for s in r["sources"]]
    ver = sum(s["verified"] for s in refs) / max(1, len(refs))
    add(f"{meeting}-EVID", "evidence", "At least 90% of requirement quotes verified in the transcript", ver >= 0.9, f"{ver:.0%} of {len(refs)}")
    crit = [c for d in pkg["scope"]["deliverables"] for c in d["acceptance_criteria"]]
    testable = sum(not (VAGUE.search(c) and not re.search(r"\d", c)) for c in crit) / max(1, len(crit))
    add(f"{meeting}-AC", "acceptance", "At least 80% of acceptance criteria are testable", testable >= 0.8, f"{testable:.0%} of {len(crit)}")
    return rows


def score_change(pkg: dict, base_pkg: dict, gold: dict, client: TestClient, vid: int) -> list[dict]:
    rows = []
    cr = pkg.get("change_report") or {"items": [], "schedule_change": None}
    items = cr["items"]
    add = lambda cid, title, ok, detail="": rows.append({"meeting": "meeting-c", "case": cid, "kind": "change", "title": title, "passed": int(bool(ok)), "detail": detail[:300]})  # noqa: E731
    for c in gold["changes"]:
        t = c["type"]
        if t == "removed":
            hit = [i for i in items if i["change_type"] == "removed" and any(x in low(i["title"], i["before"]) for x in c["terms"])]
            add(c["id"], c["title"], hit, ", ".join(i["requirement_id"] for i in hit))
        elif t == "added_or_modified":
            hit = [i for i in items if i["change_type"] in ("added", "modified") and any(x in low(i["title"], i["after"]) for x in c["terms"])]
            add(c["id"], c["title"], hit, ", ".join(f"{i['change_type']} {i['requirement_id']}" for i in hit))
        elif t == "any_change_mentions":
            hit = [i for i in items if any(x in low(i["title"], i["before"], i["after"], i["explanation"]) for x in c["terms"])]
            add(c["id"], c["title"], hit, ", ".join(i["requirement_id"] for i in hit))
        elif t == "added_out_of_scope":
            hit = [i for i in items if i["change_type"] == "added" and i["out_of_scope"] and any(x in low(i["title"], i["after"]) for x in c["terms"])]
            add(c["id"], c["title"], hit, ", ".join(i["requirement_id"] for i in hit))
        elif t == "schedule":
            sc = cr.get("schedule_change") or {}
            ok = bool(sc) and any(x in low(sc.get("before")) for x in c["before"]) and all(x in low(sc.get("after")) for x in c["after"])
            add(c["id"], c["title"], ok, json.dumps(sc))
        elif t == "no_false_removals":
            removed = [i for i in items if i["change_type"] == "removed"]
            bad = [i["requirement_id"] for i in removed if any(x in low(i["title"], i["before"]) for x in c["protected"])]
            add(c["id"], c["title"], not bad and len(removed) <= 2, f"removed {[i['requirement_id'] for i in removed]}, wrongly: {bad}")
        elif t == "kept_verbatim":
            base_ids = {d["id"] for d in base_pkg["scope"]["deliverables"]}
            kept = [d["id"] for d in pkg["scope"]["deliverables"] if d["id"] in base_ids]
            strip = lambda ts, did: [t_ for t_ in ts if t_["deliverable_id"] == did and t_["category"] != "project_management"]  # noqa: E731
            same = all(strip(base_pkg["estimate"]["tasks"], d) == strip(pkg["estimate"]["tasks"], d) for d in kept)
            add(c["id"], c["title"], same, f"{len(kept)} kept deliverable(s)")
        elif t == "approval_gate":
            v = client.get(f"/versions/{vid}").json()
            r = client.post(f"/versions/{vid}/send", json={"sender": "eval"})
            add(c["id"], c["title"], v["status"] == "draft" and r.status_code == 409, f"status {v['status']}, send -> {r.status_code}")
    return rows


def run_meeting(svc, meeting_id: str) -> tuple[int | None, dict, float]:
    t0 = time.perf_counter()
    job = svc.orchestrator.start(meeting_id)
    vid = svc.orchestrator.run_job(job)
    return vid, svc.store.job(job), time.perf_counter() - t0


def evaluate(model: str | None, review_model: str | None) -> tuple[list[dict], dict]:
    tmp = Path(tempfile.mkdtemp(prefix="scope-eval-"))
    s = Settings(db_path=tmp / "scope.sqlite3", sent_dir=tmp / "sent")
    if model:
        for k in list(s.__dict__):
            if k.startswith("model_"):
                setattr(s, k, model)
    if review_model:
        s.model_review = review_model
    label = model or "configured"
    if s.llm_provider == "fake" or not os.getenv("ANTHROPIC_API_KEY"):
        label = "fake (not Claude)"
    svc = build_services(s)
    if not svc.kb.health().get("ok"):
        raise SystemExit(f"Knowledge Brain not reachable at {s.kb_url}; start it first.")
    client = TestClient(create_app(svc))
    client.post("/demo/load")
    rows: list[dict] = []
    perf: dict = {"model": label, "meetings": {}}

    def record(meeting, vid, job, secs):
        pkg = svc.store.version(vid)["payload"] if vid else None
        perf["meetings"][meeting] = {"ok": bool(vid), "error": job.get("error"), "wall_s": round(secs, 1),
                                     "cost_usd": round(pkg["usage"]["cost_usd"], 4) if pkg else None,
                                     "model_time_s": round(pkg["usage"]["latency_ms"] / 1000, 1) if pkg else None,
                                     "calls": pkg["usage"]["calls"] if pkg else None, "retries": pkg["usage"]["retries"] if pkg else None,
                                     "kb_calls": pkg["usage"]["by_stage"].get("knowledge_brain", {}).get("calls") if pkg else None,
                                     "requirements": len(pkg["requirements"]["requirements"]) if pkg else None,
                                     "likely_hours": pkg["estimate"]["total_likely"] if pkg else None,
                                     "expected_hours": pkg["estimate"]["total_with_contingency"] if pkg else None}
        return pkg

    gold_a, gold_b, gold_c = (json.loads((GOLD / n).read_text()) for n in ("meeting_a.json", "meeting_b.json", "meeting_c.json"))
    vid_a, job, secs = run_meeting(svc, "meeting-a")
    pkg_a = record("meeting-a", vid_a, job, secs)
    if pkg_a:
        rows += score_baseline(pkg_a, gold_a, "meeting-a")
        client.post(f"/versions/{vid_a}/approve", json={"reviewer": "eval-harness", "acknowledge_blocks": True})
        vid_c, job, secs = run_meeting(svc, "meeting-c")
        pkg_c = record("meeting-c", vid_c, job, secs)
        if pkg_c:
            rows += score_change(pkg_c, pkg_a, gold_c, client, vid_c)
    vid_b, job, secs = run_meeting(svc, "meeting-b")
    pkg_b = record("meeting-b", vid_b, job, secs)
    if pkg_b:
        rows += score_baseline(pkg_b, gold_b, "meeting-b")
        r1 = client.post(f"/versions/{vid_b}/send", json={"sender": "eval"})
        rows.append({"meeting": "meeting-b", "case": "P1", "kind": "process", "title": "Unapproved proposal cannot be sent", "passed": int(r1.status_code == 409), "detail": str(r1.status_code)})
        client.post(f"/versions/{vid_b}/approve", json={"reviewer": "eval-harness", "acknowledge_blocks": True})
        t = pkg_b["estimate"]["tasks"][0]
        e = client.patch(f"/versions/{vid_b}/tasks/{t['id']}", json={"low": t["low"], "likely": t["likely"] + 1, "high": t["high"] + 1, "editor": "eval"}).json()
        r2 = client.post(f"/versions/{vid_b}/send", json={"sender": "eval"})
        rows.append({"meeting": "meeting-b", "case": "P2", "kind": "process", "title": "Editing an approved version requires re-approval",
                     "passed": int(e.get("status") == "draft" and r2.status_code == 409), "detail": f"{e.get('status')}, send -> {r2.status_code}"})
    # every gold case that could not run (pipeline failure) counts as a failure, never as "skipped"
    expected = {g["id"] for g in gold_a["requirements"]} | {c["id"] for c in gold_a["checks"]} | {g["id"] for g in gold_b["requirements"]} \
        | {c["id"] for c in gold_b["checks"]} | {c["id"] for c in gold_c["changes"]}
    seen = {r["case"] for r in rows}
    for cid in sorted(expected - seen):
        rows.append({"meeting": "?", "case": cid, "kind": "not_run", "title": "pipeline failed before this case", "passed": 0, "detail": ""})
    for r in rows:
        r["model"] = label
    return rows, perf


def summarize(rows: list[dict], perf: dict) -> dict:
    def rate(kind=None, meeting=None):
        rs = [r for r in rows if (kind is None or r["kind"] in kind) and (meeting is None or r["meeting"] == meeting)]
        return round(100 * sum(r["passed"] for r in rs) / len(rs), 1) if rs else None
    ms = perf["meetings"].values()
    return {"model": perf["model"], "cases": len(rows), "pass_rate": rate(), "requirements": rate(("requirement",)),
            "feasibility": rate(("feasibility",)), "checks": rate(("check",)), "change_detection": rate(("change",)),
            "estimates_in_band": rate(("estimate",)), "evidence": rate(("evidence",)), "acceptance_testable": rate(("acceptance",)),
            "process_gates": rate(("process",)), "cost_usd_total": round(sum(m["cost_usd"] or 0 for m in ms), 4),
            "cost_per_meeting_usd": round(sum(m["cost_usd"] or 0 for m in ms) / max(1, len(ms)), 4),
            "wall_s_per_meeting": round(sum(m["wall_s"] for m in ms) / max(1, len(ms)), 1),
            "pipeline_failures": sum(not m["ok"] for m in ms), "retries": sum(m["retries"] or 0 for m in ms),
            "meetings": perf["meetings"], "failed_cases": [f"{r['case']} ({r['detail']})" for r in rows if not r["passed"]]}


def markdown(summaries: list[dict], stamp: str) -> str:
    L = [f"_Run {stamp}: meetings A (baseline), C (follow-up vs approved A), B (baseline); gold in evaluation/gold/._", "",
         "| Model | Pass | Requirements | Platform check | Notes/constraints | Change detection | Estimate in band | Evidence verified | Testable criteria | Approval gates | Cost / meeting | Wall time / meeting | Pipeline failures |",
         "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    f = lambda x: "n/a" if x is None else f"{x}%"  # noqa: E731
    for s in summaries:
        L.append(f"| {s['model']} | {f(s['pass_rate'])} ({s['cases']}) | {f(s['requirements'])} | {f(s['feasibility'])} | {f(s['checks'])} | {f(s['change_detection'])} | "
                 f"{f(s['estimates_in_band'])} | {f(s['evidence'])} | {f(s['acceptance_testable'])} | {f(s['process_gates'])} | ${s['cost_per_meeting_usd']} | {s['wall_s_per_meeting']} s | {s['pipeline_failures']} |")
    for s in summaries:
        L.append("")
        L.append(f"**{s['model']}** estimates: " + "; ".join(f"{m}: {v['likely_hours']} h likely / {v['expected_hours']} h expected" for m, v in s["meetings"].items() if v["ok"]))
        if s["failed_cases"]:
            L.append(f"Failed: {'; '.join(s['failed_cases'][:15])}")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="", help="comma-separated; each runs the whole agent team on that model")
    ap.add_argument("--review-model", default=None)
    args = ap.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    all_rows, summaries = [], []
    for m in [x.strip() for x in args.models.split(",") if x.strip()] or [None]:
        print(f"== {m or 'configured models'}")
        rows, perf = evaluate(m, args.review_model)
        all_rows += rows
        summaries.append(summarize(rows, perf))
        for r in rows:
            print(f"  {'PASS' if r['passed'] else 'FAIL'} {r['case']:<8} {r['title'][:60]:<60} {r['detail'][:60]}")
    with (RESULTS / f"scope_cases_{stamp}.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["model", "meeting", "case", "kind", "title", "passed", "detail"])
        w.writeheader()
        w.writerows(all_rows)
    out = {"task": "meeting_to_scope", "run_at": stamp, "summaries": summaries}
    for n in (f"scope_summary_{stamp}.json", "scope_summary_latest.json"):
        (RESULTS / n).write_text(json.dumps(out, indent=2))
    md = markdown(summaries, stamp)
    (RESULTS / "scope_summary_latest.md").write_text(md)
    print("\n" + md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
