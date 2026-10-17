#!/usr/bin/env bash
# One command for everything:
#   ./start.sh              install (first run), start all three services, open http://localhost:8001
#   ./start.sh crawl [N]    ingest the help centre (N = pilot crawl of the first N articles)
#   ./start.sh test         run all unit tests (no API key or network needed)
#   ./start.sh eval [args]  run all three evaluation suites (services must be running)
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
mkdir -p logs

say() { printf "\033[1m%s\033[0m\n" "$*"; }
die() { printf "\033[31m%s\033[0m\n" "$*" >&2; exit 1; }

# ------------------------------------------------------------------ setup
command -v python3 >/dev/null || die "python3 not found (need 3.10+)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Python 3.10+ required (found $(python3 --version))"
command -v node >/dev/null || die "Node.js not found (need 18+)"
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' || die "Node 18+ required (found $(node --version))"

if [ ! -f .env ]; then
  cp .env.example .env
  say "Created .env from .env.example. Add your ANTHROPIC_API_KEY to it (or set LLM_PROVIDER=fake for a keyless demo)."
fi
KB_URL="$(grep -E '^KB_URL=' .env | tail -1 | cut -d= -f2- | tr -d '"' || true)"; KB_URL="${KB_URL:-http://127.0.0.1:8001}"

PY="$ROOT/.venv/bin/python"
REQ_STAMP=".venv/.req-$(cat task1-knowledge-brain/requirements.txt task3-meeting-to-scope/requirements.txt | cksum | cut -d' ' -f1)"
if [ ! -x "$PY" ] || [ ! -f "$REQ_STAMP" ]; then
  say "Installing Python dependencies (first run only)…"
  python3 -m venv .venv
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r task1-knowledge-brain/requirements.txt -r task3-meeting-to-scope/requirements.txt
  rm -f .venv/.req-*; touch "$REQ_STAMP"
fi
if [ ! -d task2-inbox-agent/node_modules ]; then
  say "Installing Node dependencies (first run only)…"
  (cd task2-inbox-agent && npm ci --no-audit --no-fund >/dev/null)
fi

cmd="${1:-up}"
case "$cmd" in
  test)
    say "Task 1 tests"; (cd task1-knowledge-brain && "$PY" -m pytest -q)
    say "Task 2 tests"; (cd task2-inbox-agent && npx tsc --noEmit && npx vitest run)
    say "Task 3 tests"; (cd task3-meeting-to-scope && "$PY" -m pytest -q)
    exit 0 ;;
  crawl)
    shift || true
    if [ -n "${1:-}" ]; then (cd task1-knowledge-brain && "$PY" -m kb.cli ingest --limit "$1")
    else (cd task1-knowledge-brain && "$PY" -m kb.cli ingest); fi
    exit $? ;;
  eval)
    shift || true
    curl -sf "$KB_URL/health" >/dev/null || die "Start the services first: ./start.sh"
    say "Task 1 eval"; (cd task1-knowledge-brain && "$PY" evaluation/run_eval.py "$@")
    say "Task 2 eval"; (cd task2-inbox-agent && npx tsx src/evaluation/runEval.ts)
    say "Task 3 eval"; (cd task3-meeting-to-scope && "$PY" evaluation/run_eval.py)
    "$PY" scripts/fill_evals.py
    exit 0 ;;
  up) ;;
  *) die "unknown command: $cmd (use: up | crawl [N] | test | eval)";;
esac

# ------------------------------------------------------------------- run
pids=()
cleanup() { say "Stopping…"; for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null; }
trap cleanup EXIT INT TERM

wait_for() { for _ in $(seq 1 "${3:-90}"); do curl -sf "$1" >/dev/null 2>&1 && return 0; sleep 1; done; die "$2 did not start; see logs/"; }

say "Starting Knowledge Brain on :8001 (first start downloads the embedding model, ~130 MB)…"
(cd task1-knowledge-brain && exec "$PY" -m kb.cli serve --port 8001) > logs/knowledge-brain.log 2>&1 &
pids+=($!)
wait_for http://127.0.0.1:8001/health "Knowledge Brain" 400

say "Starting Inbox Agent on :3002…"
(cd task2-inbox-agent && npm run build:ui >/dev/null && exec npx tsx src/server.ts) > logs/inbox-agent.log 2>&1 &
pids+=($!)
say "Starting Meeting to Scope on :8003…"
(cd task3-meeting-to-scope && exec "$PY" -m scope.cli serve --port 8003) > logs/meeting-to-scope.log 2>&1 &
pids+=($!)
wait_for http://127.0.0.1:3002/api/health "Inbox Agent"
wait_for http://127.0.0.1:8003/health "Meeting to Scope"

articles=$(curl -s http://127.0.0.1:8001/health | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["articles_active"])')
echo
say "All three services are up:"
echo "  Knowledge Brain   http://localhost:8001   (API docs: /docs)"
echo "  Inbox Agent       http://localhost:3002"
echo "  Meeting to Scope  http://localhost:8003   (API docs: /docs)"
echo "  Logs in ./logs/   Stop with Ctrl+C"
if [ "$articles" = "0" ]; then
  echo
  say "The knowledge base is empty. In another terminal run:  ./start.sh crawl 20   (pilot), then  ./start.sh crawl   (full)"
  echo "or use 'Content updates' on http://localhost:8001."
fi
command -v open >/dev/null && open http://localhost:8001 >/dev/null 2>&1 || true
wait
