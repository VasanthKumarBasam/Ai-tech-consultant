# AI Tech Consultant

Three connected AI systems for a small web studio that builds and supports Brilliant Directories sites:

| | What it does | Stack | URL |
| --- | --- | --- | --- |
| **Task 1: Knowledge Brain** | Crawls the [BD Solution Center](https://support.brilliantdirectories.com/support/sitemap.xml) daily, describes screenshots with Claude vision, answers with citations, sends what it can't answer to an expert queue | Python, FastAPI, SQLite + FTS5, local embeddings | http://localhost:8001 (`/docs` for the API) |
| **Task 2: Inbox Agent** | Classifies customer emails, pulls facts from Task 1's API, drafts replies, blocks unsafe promises, learns from reviewer edits, earns autonomy per category (20 unchanged sends in a row) | TypeScript, Node, Express, SQLite | http://localhost:3002 |
| **Task 3: Meeting to Scope** | Turns meeting transcripts into requirements, platform checks (via Task 1's API), scope, estimates and a proposal; diffs follow-up meetings against the **approved** baseline; nothing is sent without approval | Python agent team, FastAPI | http://localhost:8003 (`/docs`) |

Tasks 2 and 3 talk to Task 1 **only through its HTTP API** ([contract](shared-docs/kb-api.md)). Architecture and trade-offs: [DESIGN.md](DESIGN.md). Measured results: [EVALS.md](EVALS.md).

## Prerequisites
Python 3.10+, Node 18+, an [Anthropic API key](https://console.anthropic.com/). macOS or Linux.

## Run it (one command)
```bash
./start.sh            # first run installs everything, creates .env, starts all three services
```
Put your key in `.env` (`ANTHROPIC_API_KEY=...`) and run `./start.sh` again. No key yet? Set `LLM_PROVIDER=fake` in `.env` to click through every screen with a clearly labelled stand-in model.

Then fill the knowledge base (in a second terminal, while the services run):
```bash
./start.sh crawl 20   # pilot: first 20 articles, ~1 minute
./start.sh crawl      # full crawl with screenshot descriptions; polite (1 req/s), resumable, safe to re-run
```
The daily incremental update runs automatically at 02:00 while the Knowledge Brain is up (`KB_DAILY_CRON`).

## Use it
- **Knowledge** (8001): ask questions, see cited sources and screenshots, answer the expert queue, run/roll back content updates.
- **Inbox** (3002): 50 sample emails are pre-loaded. "Draft all new emails", then review: send, edit, reject or hand to an expert. Autonomy and Learning tabs show the streaks and prompt versions.
- **Scope builder** (8003): "Load sample meetings", build the scope for Meeting A, approve it, then "Compare with approved scope" on the follow-up meeting.

## Tests and evaluations
```bash
./start.sh test       # 35 + 25 + 12 unit/integration tests; no key or network needed
./start.sh eval       # all three eval suites against the running services; fills EVALS.md
```
Model comparisons, e.g. `cd task1-knowledge-brain && ../.venv/bin/python evaluation/run_eval.py --models claude-haiku-5-5,claude-sonnet-5-5`. See [EVALS.md](EVALS.md).

## Configuration
Everything is in `.env` (see [.env.example](.env.example)): models per task and stage, crawl politeness, embedding back-end, hourly rate, autonomy threshold. Secrets never leave `.env`, which is git-ignored.

## Layout
```
task1-knowledge-brain/   kb/ (ingestion, multimodal, retrieval, answering, api) · evaluation/ · tests/
task2-inbox-agent/       src/ (email, knowledge-client, drafting, validation, feedback, autonomy, ui) · data/emails.json · prompts/ · tests/
task3-meeting-to-scope/  scope/ (agents, orchestrator, store, api) · transcripts/ · evaluation/gold/ · tests/
shared-docs/             API contract, demo script
```
Email delivery and "send to client" are **simulated** (saved to `data/outbox/*.eml` and `data/sent/*.md`).
