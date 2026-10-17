"""Command line: python -m kb.cli <command>

  ingest [--mode incremental|full|rebuild] [--limit N] [--url URL ...]
  inspect URL                fetch + parse one live article and print what would be indexed (no writes)
  status                     crawl manifest + last run
  ask "question" [--mode hybrid] [--model ID]
  rollback RUN_ID
  reindex                    re-embed all chunks after changing KB_EMBEDDER
  serve [--port 8001]        run the API (+ daily scheduler if KB_SCHEDULER_ENABLED=true)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kb")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest")
    pi.add_argument("--mode", default="incremental", choices=["incremental", "full", "rebuild"])
    pi.add_argument("--limit", type=int)
    pi.add_argument("--url", action="append")
    pin = sub.add_parser("inspect")
    pin.add_argument("url")
    sub.add_parser("status")
    pa = sub.add_parser("ask")
    pa.add_argument("question")
    pa.add_argument("--mode", default=None)
    pa.add_argument("--model", default=None)
    pr = sub.add_parser("rollback")
    pr.add_argument("run_id", type=int)
    sub.add_parser("reindex")
    ps = sub.add_parser("serve")
    ps.add_argument("--port", type=int, default=8001)
    ps.add_argument("--host", default="127.0.0.1")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("kb.api:app", host=args.host, port=args.port)
        return 0

    if args.cmd == "inspect":
        return asyncio.run(_inspect(args.url))

    from .services import build_services

    svc = build_services(allow_embedder_mismatch=args.cmd == "reindex")
    if args.cmd == "ingest":
        report = asyncio.run(svc.pipeline.run(args.mode, limit=args.limit, urls=args.url))
        failures = report.pop("failures", [])
        print(json.dumps(report, indent=2))
        if failures:
            print(f"\n{len(failures)} failure(s):")
            for f in failures[:20]:
                print(f"  - {f['url']}: {f['error']}")
        return 0 if report["status"] == "completed" else 1
    if args.cmd == "status":
        pages = {r["status"]: r["n"] for r in svc.db.query("SELECT status, COUNT(*) n FROM pages GROUP BY status")}
        last = svc.db.one("SELECT id,mode,status,started_at,finished_at,counts_json FROM runs ORDER BY id DESC LIMIT 1")
        print(json.dumps({"pages": pages, **svc.store.counts(), "embedder": svc.store.embedder.name,
                          "last_run": dict(last) if last else None}, indent=2))
        return 0
    if args.cmd == "ask":
        r = svc.query.answer(args.question, mode=args.mode, model=args.model, requester="cli")
        print(f"[{r['status']}] {r['answer']}\n")
        for s in r["sources"]:
            if s["cited"]:
                print(f"  {s['id']} {s['title']} — {s['url']} ({s['kind']})")
        print(f"\nlatency {r['latency_ms']} ms · cost ${r['usage'].get('cost_usd', 0):.4f} · escalation {r['escalation_id']}")
        return 0
    if args.cmd == "rollback":
        print(json.dumps(svc.pipeline.rollback(args.run_id), indent=2))
        return 0
    if args.cmd == "reindex":
        print(f"re-embedded {svc.store.reindex()} chunks with {svc.store.embedder.name}")
        return 0
    return 1


async def _inspect(url: str) -> int:
    from .config import get_settings
    from .ingestion.crawler import Fetcher
    from .ingestion.parser import parse_article
    from .ingestion.sitemap import article_id_from_url
    from .retrieval.chunking import chunk_article

    s = get_settings()
    async with Fetcher(s) as f:
        res = await f.get(url)
    if not res.ok:
        print(f"fetch failed: {res.status} {res.error}")
        return 1
    a = parse_article(res.text, url, article_id_from_url(url))
    print(f"title: {a.title}\nmodified: {a.modified_on_text}\nhash: {a.content_hash[:16]}")
    for sec in a.sections:
        print(f"\n## {sec.heading}  ({len(sec.blocks)} blocks)")
        print(sec.text[:600])
    print(f"\nimages ({len(a.images)}):")
    for i in a.images:
        print(f"  {i.ref} [{i.section}] {i.url[:100]}\n      before: ...{i.context_before[-90:]!r}")
    chunks = chunk_article(a, s.chunk_strategy, s.chunk_max_chars, s.chunk_min_chars, s.chunk_overlap_chars)
    print(f"\n{len(chunks)} chunks: " + ", ".join(f"{c.section[:30]} ({len(c.text)} chars)" for c in chunks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
