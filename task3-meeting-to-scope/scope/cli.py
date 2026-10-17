"""python -m scope.cli <command>

  serve [--port 8003]                      run the API + Scope Builder UI
  demo                                     load the sample projects/meetings
  run <meeting-id>                         run the agent pipeline for a meeting (prints the version summary)
  approve <version-id> --reviewer NAME [--ack]
  markdown <version-id>                    print the client-ready proposal
"""
from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scope")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8003)
    s.add_argument("--host", default="127.0.0.1")
    sub.add_parser("demo")
    r = sub.add_parser("run")
    r.add_argument("meeting_id")
    a = sub.add_parser("approve")
    a.add_argument("version_id", type=int)
    a.add_argument("--reviewer", required=True)
    a.add_argument("--ack", action="store_true", help="acknowledge blocking review issues")
    m = sub.add_parser("markdown")
    m.add_argument("version_id", type=int)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("scope.api:_lazy_app", factory=True, host=args.host, port=args.port)
        return 0

    from fastapi.testclient import TestClient

    from .api import create_app

    client = TestClient(create_app())
    if args.cmd == "demo":
        print(json.dumps(client.post("/demo/load").json()["projects"], indent=2))
    elif args.cmd == "run":
        from .api import build_services

        svc = build_services()
        job = svc.orchestrator.start(args.meeting_id)
        vid = svc.orchestrator.run_job(job)
        j = svc.store.job(job)
        for st in j["stages"]:
            print(f"  {st['status']:>8}  {st['name']:<12} {st.get('detail', '')}")
        if not vid:
            print(f"FAILED: {j['error']}")
            return 1
        v = svc.store.version(vid)
        pkg = v["payload"]
        print(f"\nversion id {vid} (#{v['number']}, {v['kind']}): {len(pkg['requirements']['requirements'])} requirements, "
              f"{len(pkg['scope']['deliverables'])} deliverables, {pkg['estimate']['total_with_contingency']} h, "
              f"cost ${pkg['usage']['cost_usd']:.3f}, {pkg['usage']['latency_ms'] / 1000:.1f}s model time")
        for i in pkg["review_issues"]:
            print(f"  [{i['severity']}] {i['message']}")
    elif args.cmd == "approve":
        res = client.post(f"/versions/{args.version_id}/approve", json={"reviewer": args.reviewer, "acknowledge_blocks": args.ack})
        print(res.status_code, res.json())
    elif args.cmd == "markdown":
        print(client.get(f"/versions/{args.version_id}/markdown").text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
