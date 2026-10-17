# Demo video script (10 minutes)

Before recording: `./start.sh` running, full crawl done, `./start.sh eval` done once, inbox emails drafted ("Draft all new emails"), reviewer name filled in. Close other tabs.

**0:00-0:30 On camera.** "Small web studios answer the same product questions, emails and scoping meetings again and again. I built one Knowledge Brain over the Brilliant Directories help centre and two apps that use it only through its API: an inbox agent and a meeting-to-scope agent team. Nothing reaches a customer without a person approving it."

**0:30-3:00 Knowledge Brain (localhost:8001)**
1. Ask "Does refunding a payment cancel the member's subscription?" Point at the yellow [S1] markers and click one: the source highlights. Show a screenshot source.
2. Ask something it can't know, e.g. "Can I run a Minecraft server on my hosting?" It says it isn't in the knowledge base and opens expert task #N. Open the Expert queue, write a short answer, choose "Reusable product fact", approve, ask again: now it cites the expert answer.
3. Content updates tab: show the run history (new/changed/removed/failed), say the crawl is polite (1 req/s), resumable, and runs daily at 02:00; show the Roll back button.

**3:00-5:00 Inbox (localhost:3002)**
1. Open a how-to email (E03 PayPal). Show Evidence (K1 sources from the Knowledge Brain), "Facts stated in the reply", and "Why a person needs to check this (0/20)".
2. Edit one sentence: the Send button changes to "Send my edited version" and the note warns about the streak. Pick a reason, send. Learning tab: the word diff.
3. Open a refund email (E34, chargeback threat): risk high, flags, "always needs a person". If the draft tripped a guard, show the red blocking issue and the acknowledgement checkbox.
4. Autonomy tab: explain eligible at 20 unchanged sends, a person switches it on, any edit or a flagged audit turns it off.

**5:00-7:45 Scope builder (localhost:8003)**
1. Bayside project, Meeting A "Build scope": the 7 numbered stages run (3 transcript chunks).
2. Requirements tab: each requirement with the exact quote, line and speaker. Point at the AI search item marked unclear/optional.
3. Platform check tab: map view = platform add-on (cited article), AI search = to be confirmed, "not found doesn't mean missing".
4. Estimate tab: task hours; arithmetic and contingency are done in code. Proposal tab: the client-ready document with the DRAFT banner.
5. Try "Compare with approved scope" on Meeting C before approving: refused. Approve v1, run Meeting C. Changes tab: AI search removed, language filter, "get three quotes" flagged outside the approved scope, launch moved Mar 1 → Feb 10, hours before → after. Try "Send to client" on v2: refused until approved.

**7:45-9:00 Evidence.** Open EVALS.md: model comparison tables (quality, cost per query/email/meeting, p90 latency), the keyword baseline, and one real failure from your run with its fix.

**Failure case to show (pick one live):** stop the Knowledge Brain (Ctrl+C its process or `kill` the 8001 process), click "Redraft" on an inbox email: holding reply, red "knowledge service unavailable" block, the banner at the top. Restart it and redraft.

**9:00-10:00 Close.** What works, what's imperfect (gate thresholds not yet calibrated, simulated sending, author-written gold labels), what's next (real mail intake, reranker if retrieval misses, estimates vs actual hours).
