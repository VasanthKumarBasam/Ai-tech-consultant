"""Knowledge Brain evaluation.

    python evaluation/run_eval.py                                # retrieval modes + default answer model
    python evaluation/run_eval.py --models claude-haiku-5-5,claude-sonnet-5-5 --judge claude-opus-5-5
    python evaluation/run_eval.py --retrieval-only                 # no LLM calls, free

Runs against the ingested knowledge base (run `python -m kb.cli ingest` first).
Writes CSVs and a summary (JSON + Markdown) to evaluation/results/. Every number in
EVALS.md comes from these files; nothing is typed in by hand.

Pass rule per case:
  status matches (answered vs insufficient_evidence)
  AND for answerable cases: an expected article is cited and every must_include group is satisfied
  AND no must_not_include text appears
  AND for image cases: a screenshot (image) source from an expected article was retrieved.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402

from kb.llm import LLMError  # noqa: E402
from kb.retrieval.search import retrieve  # noqa: E402
from kb.services import build_services  # noqa: E402

RESULTS = Path(__file__).parent / "results"
CASES = Path(__file__).parent / "kb_cases.jsonl"


def load_cases(path: Path, limit: int | None) -> list[dict]:
    cases = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    extra = Path(__file__).parent / "escalated_cases.jsonl"  # questions resolved by experts become regression cases
    if extra.exists() and path == CASES:
        for l in extra.read_text().splitlines():
            if l.strip():
                c = json.loads(l)
                cases.append({"id": c["id"], "type": "escalated", "question": c["question"], "expected_status": c["expected_status"],
                              "expected_articles": [], "must_include_any": [], "must_not_include": []})
    return cases[:limit] if limit else cases


def pct(x: list[float]) -> float | None:
    """Percentage, or None when there were no cases of that kind (shown as n/a, never as 0%)."""
    return round(100 * sum(x) / len(x), 1) if x else None


def fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x}%"


def quantile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def article_hit(url: str, ids: list[str]) -> bool:
    return any(f"/articles/{i}" in url for i in ids)


# ----------------------------------------------------------------- retrieval
def eval_retrieval(svc, cases: list[dict], modes: list[str], k: int = 5) -> list[dict]:
    rows = []
    for mode in modes:
        for c in cases:
            if not c["expected_articles"]:
                continue
            t0 = time.perf_counter()
            ev = retrieve(svc.store, c["question"], mode=mode, top_k=8, candidate_k=svc.settings.candidate_k)
            ms = (time.perf_counter() - t0) * 1000
            ranks = [i for i, e in enumerate(ev, 1) if article_hit(e.url, c["expected_articles"])]
            rows.append({
                "case": c["id"], "type": c["type"], "mode": mode, "hit_at_1": int(bool(ranks) and ranks[0] == 1),
                "hit_at_5": int(bool(ranks) and ranks[0] <= k), "rr": round(1 / ranks[0], 3) if ranks else 0.0,
                "image_retrieved": int(any(e.kind == "image" and article_hit(e.url, c["expected_articles"]) for e in ev)),
                "all_expected_in_top8": int(all(any(article_hit(e.url, [a]) for e in ev) for a in c["expected_articles"])),
                "top1": ev[0].url.rsplit("/", 1)[-1][:60] if ev else "", "latency_ms": round(ms, 1),
            })
    return rows


# ------------------------------------------------------------------- answers
class Judgement(BaseModel):
    faithfulness: int = Field(ge=1, le=5, description="5 = every statement supported by the cited sources")
    answers_question: int = Field(ge=1, le=5)
    unsupported_claims: list[str] = []


JUDGE_SYSTEM = """You grade answers from a help-centre assistant. Compare the ANSWER with the SOURCES only (not your own knowledge).
faithfulness: 5 = every statement is supported by the sources; 1 = mostly unsupported. answers_question: 5 = fully answers it.
List unsupported claims verbatim. Call record_judgement."""


def judge(llm, model: str, question: str, answer: str, sources: list[dict]) -> dict:
    src = "\n".join(f"<source title=\"{s['title']}\">{s['snippet']}</source>" for s in sources if s.get("cited")) or "(none cited)"
    try:
        r = llm.structured(model=model, system=JUDGE_SYSTEM, content=f"<question>{question}</question>\n<answer>{answer}</answer>\n<sources>\n{src}\n</sources>",
                           tool_name="record_judgement", tool_description="Record the grade.", schema=Judgement.model_json_schema(), max_tokens=600)
        j = Judgement.model_validate(r.data)
        return {"judge_faithfulness": j.faithfulness, "judge_answers": j.answers_question, "judge_unsupported": "; ".join(j.unsupported_claims)[:300],
                "judge_cost_usd": r.usage.cost_usd}
    except (LLMError, ValueError) as e:
        return {"judge_faithfulness": "", "judge_answers": "", "judge_unsupported": f"judge failed: {e}"[:200], "judge_cost_usd": 0}


def eval_answers(svc, cases: list[dict], model: str, mode: str, judge_model: str | None) -> list[dict]:
    rows = []
    label = model if getattr(svc.llm, "provider", "") == "anthropic" else f"{svc.llm.provider} (not Claude)"
    for c in cases:
        try:
            r = svc.query.answer(c["question"], model=model, mode=mode, create_escalation=False, requester="eval")
        except Exception as e:  # noqa: BLE001 - recorded as a failure, never hidden
            rows.append({"case": c["id"], "type": c["type"], "model": label, "status": "exception", "passed": 0, "error": str(e)[:300]})
            continue
        answer_l = r["answer"].lower()
        cited = [s for s in r["sources"] if s["cited"]]
        status_ok = (r["status"] == "answered") == (c["expected_status"] == "answered")
        cited_ok = not c["expected_articles"] or any(article_hit(s["url"], c["expected_articles"]) for s in cited)
        all_cited = not c["expected_articles"] or all(any(article_hit(s["url"], [a]) for s in cited) for a in c["expected_articles"])
        kw_ok = all(any(term.lower() in answer_l for term in group) for group in c["must_include_any"])
        forbidden_ok = not any(x.lower() in answer_l for x in c["must_not_include"])
        image_ok = c["type"] != "image" or any(s["kind"] == "image" and article_hit(s["url"], c["expected_articles"]) for s in r["sources"])
        answerable = c["expected_status"] == "answered"
        passed = status_ok and forbidden_ok and image_ok and (not answerable or (cited_ok and kw_ok))
        row = {
            "case": c["id"], "type": c["type"], "model": label, "mode": mode, "status": r["status"], "expected_status": c["expected_status"],
            "status_ok": int(status_ok), "cited_expected": int(cited_ok), "cited_all_expected": int(all_cited), "keywords_ok": int(kw_ok),
            "forbidden_ok": int(forbidden_ok), "image_ok": int(image_ok), "passed": int(passed), "n_cited": len(cited),
            "top_similarity": r["retrieval"]["top_similarity"], "latency_ms": r["latency_ms"], "input_tokens": r["usage"].get("input_tokens", 0),
            "output_tokens": r["usage"].get("output_tokens", 0), "cost_usd": r["usage"].get("cost_usd", 0.0),
            "retries": r["usage"].get("validation_retries", 0), "error": r.get("error") or "", "answer": r["answer"][:400].replace("\n", " "),
        }
        if judge_model and r["status"] in ("answered", "partial"):
            row.update(judge(svc.llm, judge_model, c["question"], r["answer"], r["sources"]))
        rows.append(row)
        print(f"  {c['id']:<7} {label:<20} {r['status']:<22} {'PASS' if passed else 'FAIL'}  {r['latency_ms']} ms  ${row['cost_usd']:.4f}")
    return rows


def summarize_answers(rows: list[dict]) -> dict:
    out = {}
    for model in sorted({r["model"] for r in rows}):
        rs = [r for r in rows if r["model"] == model and r["status"] != "exception"]
        exc = [r for r in rows if r["model"] == model and r["status"] == "exception"]
        ans = [r for r in rs if r["expected_status"] == "answered"]
        una = [r for r in rs if r["expected_status"] != "answered"]
        lat = [r["latency_ms"] for r in rs]
        fa = [r["judge_faithfulness"] for r in rs if isinstance(r.get("judge_faithfulness"), int)]
        out[model] = {
            "cases": len(rs) + len(exc), "pass_rate": pct([r["passed"] for r in rs] + [0] * len(exc)),
            "answerable_correct": pct([r["passed"] for r in ans]), "citation_accuracy": pct([r["cited_expected"] for r in ans]),
            "keyword_coverage": pct([r["keywords_ok"] for r in ans]), "abstention_accuracy": pct([r["status_ok"] for r in una]),
            "injection_resisted": pct([r["forbidden_ok"] for r in rs if r["type"] == "injection"]),
            "image_cases": pct([r["image_ok"] for r in rs if r["type"] == "image"]),
            "median_latency_ms": round(statistics.median(lat)) if lat else 0, "p90_latency_ms": round(quantile(lat, 0.9)) if lat else 0,
            "cost_per_query_usd": round(sum(r["cost_usd"] for r in rs) / max(1, len(rs)), 5), "total_cost_usd": round(sum(r["cost_usd"] for r in rs), 4),
            "errors": len(exc) + sum(1 for r in rs if r["status"] == "error"), "retries": sum(r["retries"] for r in rs),
            "judge_faithfulness_avg": round(statistics.mean(fa), 2) if fa else None,
            "failed_cases": [r["case"] for r in rs if not r["passed"]] + [r["case"] for r in exc],
        }
    return out


def summarize_retrieval(rows: list[dict]) -> dict:
    out = {}
    for mode in sorted({r["mode"] for r in rows}):
        rs = [r for r in rows if r["mode"] == mode]
        out[mode] = {"cases": len(rs), "hit_at_1": pct([r["hit_at_1"] for r in rs]), "hit_at_5": pct([r["hit_at_5"] for r in rs]),
                     "mrr": round(statistics.mean([r["rr"] for r in rs]), 3) if rs else 0,
                     "multi_all_found": pct([r["all_expected_in_top8"] for r in rs if r["type"] == "multi"]),
                     "image_retrieved": pct([r["image_retrieved"] for r in rs if r["type"] == "image"]),
                     "median_latency_ms": round(statistics.median([r["latency_ms"] for r in rs]), 1) if rs else 0}
    return out


def ingestion_summary(svc) -> dict:
    pages = {r["status"]: r["n"] for r in svc.db.query("SELECT status, COUNT(*) n FROM pages GROUP BY status")}
    runs = svc.db.query("SELECT id, mode, status, counts_json FROM runs WHERE status IN ('completed','failed') ORDER BY id DESC LIMIT 5")
    last = []
    for r in runs:
        c = json.loads(r["counts_json"] or "{}")
        last.append({"run": r["id"], "mode": r["mode"], "status": r["status"], **{k: c.get(k) for k in
                     ("discovered", "new", "changed", "unchanged", "removed", "failed", "duration_s", "requests_made")},
                     "images_described": (c.get("images") or {}).get("described"), "image_cost_usd": round((c.get("images") or {}).get("cost_usd", 0), 4)})
    total = sum(pages.values())
    return {"pages": pages, "page_success_rate": pct([1] * pages.get("indexed", 0) + [0] * pages.get("failed", 0)) if total else None,
            "chunks": svc.store.counts(), "embedder": svc.store.embedder.name, "recent_runs": last}


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def markdown(summary: dict) -> str:
    lines = [f"_Run {summary['run_at']} on {summary['ingestion']['chunks'].get('chunks_active')} active chunks "
             f"({summary['ingestion']['embedder']}), {summary['n_cases']} cases._", ""]
    if summary["retrieval"]:
        lines += ["**Retrieval (no LLM)**", "", "| Mode | Hit@1 | Hit@5 | MRR | Multi-article: all found | Screenshot retrieved | Median ms |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for m, s in summary["retrieval"].items():
            lines.append(f"| {m} | {fmt(s['hit_at_1'])} | {fmt(s['hit_at_5'])} | {s['mrr']} | {fmt(s['multi_all_found'])} | {fmt(s['image_retrieved'])} | {s['median_latency_ms']} |")
        lines.append("")
    if summary["answers"]:
        lines += ["**Answers by model**", "", "| Model | Pass | Answerable correct | Citation accuracy | Abstention | Injection resisted | Faithfulness (judge) | Median / p90 latency | Cost / query | Errors |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for m, s in summary["answers"].items():
            lines.append(f"| {m} | {fmt(s['pass_rate'])} | {fmt(s['answerable_correct'])} | {fmt(s['citation_accuracy'])} | {fmt(s['abstention_accuracy'])} | {fmt(s['injection_resisted'])} | "
                         f"{s['judge_faithfulness_avg'] if s['judge_faithfulness_avg'] is not None else 'n/a'} | {s['median_latency_ms']} / {s['p90_latency_ms']} ms | ${s['cost_per_query_usd']} | {s['errors']} |")
        lines.append("")
        for m, s in summary["answers"].items():
            if s["failed_cases"]:
                lines.append(f"- Failed with {m}: {', '.join(s['failed_cases'])}")
    ing = summary["ingestion"]
    if ing["recent_runs"]:
        r = ing["recent_runs"][0]
        lines += ["", f"**Ingestion:** pages {ing['pages']}; latest run #{r['run']} ({r['mode']}, {r['status']}): {r['new']} new, {r['changed']} changed, "
                  f"{r['unchanged']} unchanged, {r['removed']} removed, {r['failed']} failed in {r['duration_s']} s, {r['images_described']} screenshots described (${r['image_cost_usd']})."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=None, help="comma-separated answer models (default: KB_ANSWER_MODEL)")
    ap.add_argument("--modes", default="vector,keyword,hybrid")
    ap.add_argument("--answer-mode", default="hybrid")
    ap.add_argument("--judge", default=None, help="optional judge model for faithfulness")
    ap.add_argument("--retrieval-only", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--cases", default=str(CASES))
    args = ap.parse_args()

    svc = build_services()
    cases = load_cases(Path(args.cases), args.limit)
    if not svc.store.counts()["chunks_active"]:
        print("The knowledge base is empty. Run `python -m kb.cli ingest` first.")
        return 1
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    print(f"Retrieval eval: {len(cases)} cases x modes {args.modes}")
    ret = eval_retrieval(svc, cases, args.modes.split(","))
    write_csv(RESULTS / f"kb_retrieval_{stamp}.csv", ret)
    ans: list[dict] = []
    if not args.retrieval_only:
        if not getattr(svc.llm, "configured", False):
            print("No ANTHROPIC_API_KEY: skipping answer evaluation (use --retrieval-only to silence this).")
        else:
            for model in (args.models or svc.settings.answer_model).split(","):
                print(f"Answer eval with {model}")
                ans += eval_answers(svc, cases, model.strip(), args.answer_mode, args.judge)
            write_csv(RESULTS / f"kb_answers_{stamp}.csv", ans)
    summary = {"task": "knowledge_brain", "run_at": stamp, "n_cases": len(cases), "retrieval": summarize_retrieval(ret),
               "answers": summarize_answers(ans) if ans else {}, "ingestion": ingestion_summary(svc), "answer_mode": args.answer_mode}
    for name in (f"kb_summary_{stamp}.json", "kb_summary_latest.json"):
        (RESULTS / name).write_text(json.dumps(summary, indent=2))
    (RESULTS / "kb_summary_latest.md").write_text(markdown(summary))
    print("\n" + markdown(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
