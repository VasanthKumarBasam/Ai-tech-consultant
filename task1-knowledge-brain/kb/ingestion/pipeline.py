"""Ingestion pipeline: sitemap -> manifest diff -> fetch -> parse -> images -> chunk -> embed -> index.

Modes: incremental (daily; skips pages whose sitemap lastmod is unchanged and uses
conditional GETs), full (re-fetch everything, still idempotent by content hash),
rebuild (re-fetch and re-chunk/re-embed even unchanged pages).

Guarantees
* Resumable: each page's status/attempts/result is committed as soon as it is
  processed, so an interrupted run simply continues with the pages that are
  not yet `indexed` next time.
* Idempotent: a page whose extracted content hash is unchanged creates no new
  version and no new chunks.
* Failure isolation: one bad page is recorded as `failed` and the run goes on.
* Safe removals: pages missing from the sitemap are deactivated (never served
  again) but kept for rollback; a run refuses to remove more than
  KB_MAX_REMOVAL_RATIO of the corpus (protects against a broken sitemap).
* Rollback: every run's new versions can be reverted with `rollback(run_id)`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..db import Database
from ..multimodal.images import ImageDescriber, ImageDescription
from ..retrieval.chunking import chunk_article
from ..retrieval.store import ChunkStore
from .crawler import Fetcher
from .parser import Article, parse_article
from .sitemap import SitemapEntry, article_id_from_url, filter_articles, parse_sitemap

log = logging.getLogger("kb.ingest")

_run_lock = asyncio.Lock()


class RunInProgress(RuntimeError):
    pass


@dataclass
class RunReport:
    run_id: int
    mode: str
    discovered: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0
    skipped_robots: int = 0
    removal_blocked: bool = False
    chunks_written: int = 0
    images: dict = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)
    duration_s: float = 0.0
    requests_made: int = 0

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def _uid(url: str, article_id: str | None) -> str:
    return article_id or hashlib.sha1(url.encode()).hexdigest()[:12]


def image_chunk_text(article: Article, d: ImageDescription, section: str, context: str) -> str:
    parts = [f"Screenshot {d.ref} in '{article.title}' (section: {section})."]
    parts.append(f"Shows: {d.description}")
    if d.ui_labels:
        parts.append("Visible labels: " + "; ".join(d.ui_labels))
    if d.uncertainty:
        parts.append(f"Uncertain: {d.uncertainty}")
    if context:
        parts.append(f"Nearby article text: {context}")
    return "\n".join(parts)


class IngestionPipeline:
    def __init__(self, settings: Settings, db: Database, store: ChunkStore, llm, fetcher: Fetcher | None = None):
        self.s = settings
        self.db = db
        self.store = store
        self.llm = llm
        self._fetcher = fetcher

    # ------------------------------------------------------------------ run
    def mark_interrupted(self) -> None:
        """Runs still 'running' with no heartbeat for 10 minutes died with their process."""
        self.db.execute(
            """UPDATE runs SET status='interrupted', finished_at=datetime('now')
               WHERE status='running' AND COALESCE(heartbeat_at, started_at) < datetime('now', '-10 minutes')"""
        )

    def active_run(self) -> dict | None:
        row = self.db.one("SELECT id, started_at FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1")
        return dict(row) if row else None

    async def run(self, mode: str = "incremental", limit: int | None = None, urls: list[str] | None = None) -> dict:
        if _run_lock.locked():
            raise RunInProgress("An ingestion run is already in progress")
        async with _run_lock:
            self.mark_interrupted()  # anything still 'running' without a heartbeat died with a previous process
            other = self.active_run()
            if other:
                raise RunInProgress(f"Ingestion run #{other['id']} (started {other['started_at']} UTC) is still running in another process")
            run_id = self.db.execute(
                "INSERT INTO runs(mode,status,params_json) VALUES(?, 'running', ?)",
                (mode, json.dumps({"limit": limit, "urls": urls})),
            )
            report = RunReport(run_id=run_id, mode=mode)
            t0 = time.perf_counter()
            fetcher = self._fetcher or Fetcher(self.s)
            try:
                await self._run(fetcher, report, mode, limit, urls)
                status, error = "completed", None
            except Exception as e:  # the run fails, but every committed page stays committed
                log.exception("ingestion run failed")
                status, error = "failed", f"{type(e).__name__}: {e}"
            finally:
                report.duration_s = round(time.perf_counter() - t0, 1)
                report.requests_made = fetcher.requests_made
                if self._fetcher is None:
                    await fetcher.close()
            self.db.execute(
                "UPDATE runs SET status=?, finished_at=datetime('now'), counts_json=?, error=? WHERE id=?",
                (status, json.dumps(report.as_dict()), error, run_id),
            )
            out = report.as_dict()
            out["status"] = status
            out["error"] = error
            return out

    async def _sitemap_entries(self, fetcher: Fetcher) -> list[SitemapEntry]:
        todo, seen, entries = [self.s.sitemap_url], set(), []
        while todo:
            url = todo.pop()
            if url in seen:
                continue
            seen.add(url)
            res = await fetcher.get(url)
            if not res.ok:
                raise RuntimeError(f"sitemap fetch failed for {url}: {res.error}")
            got, children = parse_sitemap(res.text)
            entries.extend(got)
            todo.extend(children)
        return filter_articles(entries, self.s.article_url_pattern)

    async def _run(self, fetcher: Fetcher, report: RunReport, mode: str, limit: int | None, urls: list[str] | None) -> None:
        entries = await self._sitemap_entries(fetcher)
        full_listing = urls is None and limit is None
        if urls:
            wanted = set(urls)
            entries = [e for e in entries if e.url in wanted] + [
                SitemapEntry(u, None) for u in urls if u not in {e.url for e in entries}
            ]
        if limit:
            entries = entries[:limit]
        report.discovered = len(entries)

        to_process: list[tuple[SitemapEntry, dict | None]] = []
        for e in entries:
            row = self.db.one("SELECT * FROM pages WHERE url=?", (e.url,))
            if row is None:
                self.db.execute(
                    "INSERT INTO pages(url,article_id,lastmod,status,first_seen_run,last_seen_run) VALUES(?,?,?,?,?,?)",
                    (e.url, article_id_from_url(e.url), e.lastmod, "pending", report.run_id, report.run_id),
                )
                to_process.append((e, None))
                continue
            row = dict(row)
            self.db.execute("UPDATE pages SET last_seen_run=? WHERE url=?", (report.run_id, e.url))
            unchanged_meta = row["status"] == "indexed" and row["active"] and (row["lastmod"] == e.lastmod) and e.lastmod
            if mode == "incremental" and unchanged_meta:
                report.unchanged += 1
            elif row["status"] == "failed" and row["attempts"] >= self.s.max_page_attempts and mode == "incremental":
                report.failed += 1
                report.failures.append({"url": e.url, "error": f"gave up after {row['attempts']} attempts: {row['last_error']}"})
            else:
                to_process.append((e, row))

        describer = ImageDescriber(self.s, self.db, self.llm, fetcher, concurrency=self.s.vision_concurrency)
        await asyncio.gather(*(self._process(fetcher, describer, e, row, report, mode) for e, row in to_process))
        report.images = describer.stats

        if full_listing:
            self._handle_removals({e.url for e in entries}, report)

    # --------------------------------------------------------------- pages
    async def _process(self, fetcher: Fetcher, describer: ImageDescriber, e: SitemapEntry, row: dict | None,
                       report: RunReport, mode: str) -> None:
        url = e.url
        self.db.execute("UPDATE runs SET heartbeat_at=datetime('now') WHERE id=?", (report.run_id,))
        try:
            use_conditional = mode == "incremental" and row and row["status"] == "indexed" and row["active"]
            res = await fetcher.get(
                url,
                etag=row["etag"] if use_conditional else None,
                last_modified=row["last_modified_header"] if use_conditional else None,
            )
            self.db.execute(
                "UPDATE pages SET attempts=attempts+1, http_status=?, last_fetched_at=datetime('now'), updated_at=datetime('now') WHERE url=?",
                (res.status, url),
            )
            if res.error == "disallowed by robots.txt":
                self.db.execute("UPDATE pages SET status='skipped_robots', last_error=? WHERE url=?", (res.error, url))
                report.skipped_robots += 1
                return
            if res.not_modified:
                self.db.execute("UPDATE pages SET lastmod=?, attempts=0 WHERE url=?", (e.lastmod, url))
                report.unchanged += 1
                return
            if not res.ok:
                if res.status in (404, 410):
                    self._deactivate_page(url, report.run_id)
                    report.removed += 1
                    return
                self.db.execute("UPDATE pages SET status='failed', last_error=? WHERE url=?", (res.error, url))
                report.failed += 1
                report.failures.append({"url": url, "error": res.error})
                return

            article = parse_article(res.text, url, article_id_from_url(url))
            if not article.sections:
                raise ValueError("no article content found")
            current = self.db.one("SELECT content_hash, active_version_id, active FROM pages WHERE url=?", (url,))
            same = current and current["content_hash"] == article.content_hash and current["active"] and current["active_version_id"]
            if same and mode != "rebuild":  # "rebuild" re-chunks/re-embeds unchanged pages (e.g. new chunking strategy)
                self.db.execute(
                    "UPDATE pages SET status='indexed', lastmod=?, etag=?, last_modified_header=?, attempts=0, last_error=NULL WHERE url=?",
                    (e.lastmod, res.headers.get("etag"), res.headers.get("last-modified"), url),
                )
                report.unchanged += 1
                return

            descriptions = await describer.describe_article(article)
            n = await asyncio.to_thread(self._index_article, article, descriptions, e, res.headers, report.run_id)
            report.chunks_written += n
            if row is None or not row.get("active_version_id"):
                report.new += 1
            else:
                report.changed += 1
        except Exception as ex:  # failure isolation
            log.warning("page failed %s: %s", url, ex)
            self.db.execute(
                "UPDATE pages SET status='failed', last_error=? WHERE url=?", (f"{type(ex).__name__}: {ex}"[:500], url)
            )
            report.failed += 1
            report.failures.append({"url": url, "error": str(ex)[:300]})

    def _index_article(self, article: Article, descriptions: list[ImageDescription], e: SitemapEntry,
                       headers: dict, run_id: int) -> int:
        by_ref = {d.ref: d for d in descriptions}
        img_meta = {i.ref: i for i in article.images}
        version_id = self.db.execute(
            """INSERT INTO article_versions(url,article_id,title,body_text,sections_json,images_json,content_hash,lastmod,
                                            modified_on_text,run_id,is_active) VALUES(?,?,?,?,?,?,?,?,?,?,0)""",
            (article.url, article.article_id, article.title, article.body_text, article.sections_json(),
             json.dumps([{**img_meta[r].__dict__, "description": by_ref[r].description if r in by_ref else None,
                          "ui_labels": by_ref[r].ui_labels if r in by_ref else []} for r in img_meta]),
             article.content_hash, e.lastmod, article.modified_on_text, run_id),
        )
        uid = _uid(article.url, article.article_id)
        rows = []
        for c in chunk_article(article, self.s.chunk_strategy, self.s.chunk_max_chars, self.s.chunk_min_chars, self.s.chunk_overlap_chars):
            text = c.text
            for ref in c.image_refs:  # make text chunks image-aware with a short inline description
                d = by_ref.get(ref)
                if d and d.analysed:
                    short = d.description[:220] + ("; labels: " + ", ".join(d.ui_labels[:8]) if d.ui_labels else "")
                    text = text.replace(f"[Screenshot {ref}]", f"[Screenshot {ref}: {short}]")
            rows.append({
                "chunk_id": f"{uid}:v{version_id}:c{c.index}", "version_id": version_id, "url": article.url,
                "article_id": article.article_id, "title": article.title, "section": c.section, "kind": "text",
                "text": text, "image_refs": c.image_refs, "embed_text": f"{article.title} > {c.section}\n{text}",
                "image_url": img_meta[c.image_refs[0]].url if c.image_refs else None,
            })
        for d in descriptions:
            if not d.analysed:
                continue  # never index a placeholder as if it were image evidence
            meta = img_meta[d.ref]
            ctx = (meta.context_before[-200:] + " " + meta.context_after[:120]).strip()
            text = image_chunk_text(article, d, meta.section, ctx)
            rows.append({
                "chunk_id": f"{uid}:v{version_id}:{d.ref.lower()}", "version_id": version_id, "url": article.url,
                "article_id": article.article_id, "title": article.title, "section": meta.section, "kind": "image",
                "text": text, "image_url": d.url, "image_refs": [d.ref], "embed_text": text,
            })
        self.store.add_chunks(rows)
        # atomically switch the active version
        prev = self.db.one("SELECT active_version_id FROM pages WHERE url=?", (article.url,))
        if prev and prev["active_version_id"]:
            self.store.set_active("version_id=?", (prev["active_version_id"],), False)
            self.db.execute("UPDATE article_versions SET is_active=0 WHERE id=?", (prev["active_version_id"],))
        self.db.execute("UPDATE article_versions SET is_active=1 WHERE id=?", (version_id,))
        self.db.execute(
            """UPDATE pages SET status='indexed', active=1, active_version_id=?, content_hash=?, lastmod=?, etag=?,
                   last_modified_header=?, attempts=0, last_error=NULL, removed_in_run=NULL, updated_at=datetime('now')
               WHERE url=?""",
            (version_id, article.content_hash, e.lastmod, headers.get("etag"), headers.get("last-modified"), article.url),
        )
        return len(rows)

    # ------------------------------------------------------------ removals
    def _deactivate_page(self, url: str, run_id: int) -> None:
        row = self.db.one("SELECT active_version_id FROM pages WHERE url=?", (url,))
        if row and row["active_version_id"]:
            self.store.set_active("version_id=?", (row["active_version_id"],), False)
        self.db.execute(
            "UPDATE pages SET status='removed', active=0, removed_in_run=?, updated_at=datetime('now') WHERE url=?",
            (run_id, url),
        )

    def _handle_removals(self, present: set[str], report: RunReport) -> None:
        active = [r["url"] for r in self.db.query("SELECT url FROM pages WHERE active=1")]
        missing = [u for u in active if u not in present]
        if not missing:
            return
        if active and len(missing) / len(active) > self.s.max_removal_ratio:
            report.removal_blocked = True
            report.failures.append({
                "url": self.s.sitemap_url,
                "error": f"refused to remove {len(missing)}/{len(active)} pages (> {self.s.max_removal_ratio:.0%}); check the sitemap",
            })
            return
        for u in missing:
            self._deactivate_page(u, report.run_id)
            report.removed += 1

    # ------------------------------------------------------------ rollback
    def rollback(self, run_id: int) -> dict:
        run = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise KeyError(f"run {run_id} not found")
        reverted, restored = 0, 0
        for v in self.db.query("SELECT id, url FROM article_versions WHERE run_id=? AND is_active=1", (run_id,)):
            self.store.set_active("version_id=?", (v["id"],), False)
            self.db.execute("UPDATE article_versions SET is_active=0 WHERE id=?", (v["id"],))
            prev = self.db.one(
                "SELECT id, content_hash FROM article_versions WHERE url=? AND id<? ORDER BY id DESC LIMIT 1", (v["url"], v["id"])
            )
            if prev:
                self.store.set_active("version_id=?", (prev["id"],), True)
                self.db.execute("UPDATE article_versions SET is_active=1 WHERE id=?", (prev["id"],))
                self.db.execute(
                    "UPDATE pages SET active_version_id=?, content_hash=?, active=1, status='indexed' WHERE url=?",
                    (prev["id"], prev["content_hash"], v["url"]),
                )
            else:
                self.db.execute(
                    "UPDATE pages SET active_version_id=NULL, content_hash=NULL, active=0, status='pending' WHERE url=?",
                    (v["url"],),
                )
            reverted += 1
        for p in self.db.query("SELECT url, active_version_id FROM pages WHERE removed_in_run=?", (run_id,)):
            if p["active_version_id"]:
                self.store.set_active("version_id=?", (p["active_version_id"],), True)
                self.db.execute(
                    "UPDATE pages SET active=1, status='indexed', removed_in_run=NULL WHERE url=?", (p["url"],)
                )
                restored += 1
        self.db.execute("UPDATE runs SET status='rolled_back' WHERE id=?", (run_id,))
        return {"run_id": run_id, "versions_reverted": reverted, "pages_restored": restored}
