import pytest

from conftest import HOST
from kb.retrieval.search import retrieve

REFUND = f"{HOST}/support/solutions/articles/5000674165-how-refunds-work"
CANCEL = f"{HOST}/support/solutions/articles/5000567728-how-do-i-cancel-payment-subscriptions-orders"
GATEWAY = f"{HOST}/support/solutions/articles/5000673830-finance-payment-settings-setting-up-a-payment-gateway"


def count(svc, sql):
    return svc.db.one(sql)[0]


async def test_first_run_indexes_all_articles_with_images(services):
    r = await services.pipeline.run("incremental")
    assert r["status"] == "completed", r
    assert r["discovered"] == 4 and r["new"] == 4 and r["failed"] == 0
    assert count(services, "SELECT COUNT(*) FROM pages WHERE active=1") == 4
    assert count(services, "SELECT COUNT(*) FROM chunks WHERE kind='image' AND is_active=1") == 5
    assert r["images"]["described"] == 5
    # text chunks get the inline screenshot description
    t = services.db.one("SELECT text FROM chunks WHERE kind='text' AND text LIKE '%[Screenshot IMG-1:%' LIMIT 1")
    assert t is not None


async def test_second_run_is_idempotent(services, site):
    await services.pipeline.run("incremental")
    chunks_before = count(services, "SELECT COUNT(*) FROM chunks")
    versions_before = count(services, "SELECT COUNT(*) FROM article_versions")
    hits_before = sum(site.hits.get(u, 0) for u in site.pages)
    r = await services.pipeline.run("incremental")
    assert r["unchanged"] == 4 and r["new"] == 0 and r["changed"] == 0
    assert count(services, "SELECT COUNT(*) FROM chunks") == chunks_before
    assert count(services, "SELECT COUNT(*) FROM article_versions") == versions_before
    # unchanged sitemap lastmod -> article pages are not even fetched again
    assert sum(site.hits.get(u, 0) for u in site.pages) == hits_before
    # full mode refetches, but the content hash keeps it idempotent; images come from cache
    r = await services.pipeline.run("full")
    assert r["unchanged"] == 4 and r["new"] == 0
    assert count(services, "SELECT COUNT(*) FROM chunks") == chunks_before
    assert r["images"]["described"] == 0


async def test_changed_article_creates_new_version_and_retires_old_chunks(services, site):
    await services.pipeline.run("incremental")
    site.pages[REFUND] = site.pages[REFUND].replace("six months", "ninety days")
    site.sitemap = site.sitemap.replace("2026-09-01T10:00:00Z", "2026-10-10T10:00:00Z")
    r = await services.pipeline.run("incremental")
    assert r["changed"] == 1 and r["unchanged"] == 3
    hits = retrieve(services.store, "how long does PayPal allow refunds", top_k=3)
    texts = " ".join(h.text for h in hits)
    assert "ninety days" in texts and "six months" not in texts
    assert count(services, f"SELECT COUNT(*) FROM article_versions WHERE url='{REFUND}'") == 2


async def test_lastmod_bump_without_content_change_is_unchanged(services, site):
    await services.pipeline.run("incremental")
    site.etag_support = False
    site.sitemap = site.sitemap.replace("2026-09-02T10:00:00Z", "2026-10-11T10:00:00Z")
    r = await services.pipeline.run("incremental")
    assert r["unchanged"] == 4 and r["changed"] == 0


async def test_removed_article_is_deactivated_and_not_retrieved(services, site):
    await services.pipeline.run("incremental")
    site.sitemap = "\n".join(l for l in site.sitemap.splitlines() if "cancel-payment" not in l)
    r = await services.pipeline.run("incremental")
    assert r["removed"] == 1
    hits = retrieve(services.store, "End Subscription Transaction History", top_k=8)
    assert all(h.url != CANCEL for h in hits)


async def test_removal_guard_blocks_mass_deletion(services, site):
    await services.pipeline.run("incremental")
    site.sitemap = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
    r = await services.pipeline.run("incremental")
    assert r["removal_blocked"] is True and r["removed"] == 0
    assert count(services, "SELECT COUNT(*) FROM pages WHERE active=1") == 4


async def test_transient_errors_are_retried(services, site):
    site.fail_plan[REFUND] = [503, -1]  # one 503, one connection error, then success
    r = await services.pipeline.run("incremental")
    assert r["new"] == 4 and r["failed"] == 0
    assert site.hits[REFUND] == 3


async def test_failure_is_isolated_and_resumes_next_run(services, site):
    site.fail_plan[GATEWAY] = [503, 503, 503, 503]  # exceeds max_retries=2 in this run
    r = await services.pipeline.run("incremental")
    assert r["new"] == 3 and r["failed"] == 1
    row = services.db.one("SELECT status, attempts, last_error FROM pages WHERE url=?", (GATEWAY,))
    assert row["status"] == "failed" and "503" in row["last_error"]
    r2 = await services.pipeline.run("incremental")  # resumes: only the failed page is processed
    assert r2["new"] == 1 and r2["unchanged"] == 3 and r2["failed"] == 0


async def test_interrupted_run_is_marked_and_pending_pages_resume(services, site):
    # simulate a crash: a run row left 'running' and a page left 'pending'
    services.db.execute("INSERT INTO runs(mode,status) VALUES('incremental','running')")
    services.db.execute("INSERT INTO pages(url,status,lastmod) VALUES(?, 'pending', '2026-09-01T10:00:00Z')", (REFUND,))
    r = await services.pipeline.run("incremental")
    assert services.db.one("SELECT status FROM runs WHERE id=1")["status"] == "interrupted"
    assert r["new"] == 4


async def test_404_marks_removed(services, site):
    await services.pipeline.run("incremental")
    del site.pages[GATEWAY]
    site.sitemap = site.sitemap.replace("2026-09-03T10:00:00Z", "2026-10-12T10:00:00Z")
    r = await services.pipeline.run("incremental")
    assert r["removed"] == 1
    assert services.db.one("SELECT status FROM pages WHERE url=?", (GATEWAY,))["status"] == "removed"


async def test_robots_disallow_is_respected(services, site):
    site.robots = "User-agent: *\nDisallow: /support/solutions/articles/5000674165\n"
    r = await services.pipeline.run("incremental")
    assert r["skipped_robots"] == 1 and r["new"] == 3


async def test_rollback_restores_previous_version(services, site):
    await services.pipeline.run("incremental")
    site.pages[REFUND] = site.pages[REFUND].replace("six months", "ninety days")
    site.sitemap = site.sitemap.replace("2026-09-01T10:00:00Z", "2026-10-10T10:00:00Z")
    r = await services.pipeline.run("incremental")
    out = services.pipeline.rollback(r["run_id"])
    assert out["versions_reverted"] == 1
    hits = retrieve(services.store, "how long does PayPal allow refunds", top_k=3)
    assert "six months" in " ".join(h.text for h in hits)


async def test_pilot_limit_never_removes(services, site):
    await services.pipeline.run("incremental")
    r = await services.pipeline.run("incremental", limit=1)
    assert r["discovered"] == 1 and r["removed"] == 0
    assert count(services, "SELECT COUNT(*) FROM pages WHERE active=1") == 4


@pytest.mark.parametrize("mode", ["vector", "keyword", "hybrid"])
async def test_retrieval_modes_find_the_right_article(services, mode):
    await services.pipeline.run("incremental")
    hits = retrieve(services.store, "Add New Payment Gateway drop-down Stripe", mode=mode, top_k=3)
    assert hits and hits[0].url == GATEWAY
