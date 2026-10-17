# AI coding log

**Assistant used:** Claude (Claude Code harness, model `claude-opus-5-5`), one continuous session on 2026-10-17. The full conversation is retained in the Claude app and can be shown in the live review. Commits in this repo carry a `Claude-Session` trailer linking to it.

> Fill in the entries marked ✍️ from your own work. The brief asks for real prompts and a real rejection, so don't paste anything you didn't actually do.

## Five useful prompts
1. **Scoping the whole build** (real, first message of the session). I pasted the Business Labs capstone guide (problem statement, the three tasks, rubric, submission checklist) and chose "Build the full repo" with a next-day deadline. Why it worked: giving the complete brief and rubric up front meant the architecture (shared KB behind an API, typed clients, human approval in the data layer, evals planned before code) followed the scoring criteria instead of being retrofitted.
2. ✍️ _e.g. after the first real crawl:_ "Run `python -m kb.cli inspect <url>` on these 3 articles and fix anything the parser drops (tables, nested lists, screenshots)."
3. ✍️ _e.g. after `./start.sh eval`:_ "Here are the failed KB cases from `kb_answers_<ts>.csv`. Classify each failure as retrieval, prompt or data, and propose the smallest fix; re-run only those cases."
4. ✍️ _e.g._ "Compare Haiku and Sonnet drafts for the 20 held-out emails on unsafe-draft rate, key facts and cost; recommend one with the trade-off in two sentences."
5. ✍️ _e.g._ "Calibrate `KB_MIN_SIMILARITY` on the unanswerable vs answerable cases and show the precision/recall at each threshold."

## One AI suggestion I rejected
✍️ Describe a real case from your review: what the AI proposed, why it was wrong for this project, and what you did instead. Candidates worth checking in this session's history: an AI-generated estimate you'd adjust as a delivery lead (Scope builder → Estimate tab), a draft reply that promised too much, or a design choice in DESIGN.md you disagree with.

Within the build itself, these AI-produced pieces were caught and changed before commit (see EVALS.md §7): the refund-promise regex that flagged a correct "passed to billing" sentence, title extraction that could pick the site header, and stand-in eval runs labelled with Claude model names.
