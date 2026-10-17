from conftest import FIX, HOST
from kb.ingestion.parser import parse_article
from kb.ingestion.sitemap import filter_articles, parse_sitemap
from kb.retrieval.chunking import chunk_article

REFUND_URL = f"{HOST}/support/solutions/articles/5000674165-how-refunds-work"


def refund_html() -> str:
    return (FIX / "5000674165-how-refunds-work.html").read_text()


def test_sitemap_filters_articles_and_dedupes():
    entries, children = parse_sitemap((FIX / "sitemap.xml").read_text())
    assert children == []
    arts = filter_articles(entries, r"/support/solutions/articles/\d+")
    assert len(arts) == 4  # folder/home pages dropped, '#dup' fragment collapses onto the same URL
    assert all(a.lastmod for a in arts)


def test_sitemap_index_children():
    xml = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://x.test/a.xml</loc></sitemap></sitemapindex>"""
    entries, children = parse_sitemap(xml)
    assert entries == [] and children == ["https://x.test/a.xml"]


def test_parser_extracts_title_body_and_strips_noise():
    a = parse_article(refund_html(), REFUND_URL, "5000674165")
    assert a.title == "How Refunds work"
    body = a.body_text
    assert "PayPal allows refunds for six months" in body
    assert "Did you find it helpful" not in body          # vote widget
    assert "Related Articles" not in body                  # related list
    assert "Copyright footer" not in body                  # footer
    assert a.modified_on_text and a.modified_on_text.startswith("Tue, 4 Jun, 2024")
    headings = [s.heading for s in a.sections]
    assert "How to refund a payment" in headings and "Existing Subscriptions" in headings


def test_parser_keeps_lists_tables_and_image_context():
    a = parse_article(refund_html(), REFUND_URL, "5000674165")
    steps = next(s for s in a.sections if s.heading == "How to refund a payment")
    kinds = [b.type for b in steps.blocks]
    assert "list" in kinds and kinds.count("image") == 2
    lst = next(b for b in steps.blocks if b.type == "list")
    assert lst.text.startswith("1. Go to My Members")
    table = next(b for s in a.sections for b in s.blocks if b.type == "table")
    assert table.text.splitlines()[0] == "| Gateway | Refund window |"
    # only article screenshots, not the logo / banner / tracking pixel
    assert len(a.images) == 2
    first = a.images[0]
    assert first.section == "How to refund a payment"
    assert "Payment History" in first.context_before
    assert "Refund Payment" in first.context_after
    assert "?" not in first.key  # cache key ignores query strings


def test_content_hash_is_stable_and_query_insensitive():
    h1 = parse_article(refund_html(), REFUND_URL).content_hash
    h2 = parse_article(refund_html().replace("?1644963666", "?999"), REFUND_URL).content_hash
    h3 = parse_article(refund_html().replace("six months", "90 days"), REFUND_URL).content_hash
    assert h1 == h2 and h1 != h3


def test_heading_chunking_keeps_intro_with_list_and_screenshots():
    a = parse_article(refund_html(), REFUND_URL)
    chunks = chunk_article(a, "heading", max_chars=1800, min_chars=120)
    step_chunk = next(c for c in chunks if "Follow these steps:" in c.text)
    assert "1. Go to My Members" in step_chunk.text      # list stayed with "Follow these steps:"
    assert step_chunk.image_refs == ["IMG-1", "IMG-2"]
    assert all(c.section for c in chunks)


def test_long_section_splits_with_overlap_and_table_header_repeats():
    rows = "".join(f"<tr><td>Gateway {i}</td><td>{'x' * 60}</td></tr>" for i in range(60))
    html = f"<html><body><article id='article-body'><h2>Big table</h2><table><tr><th>Name</th><th>Notes</th></tr>{rows}</table></article></body></html>"
    a = parse_article(html, "https://x.test/support/solutions/articles/1-big")
    chunks = chunk_article(a, "heading", max_chars=1500, min_chars=50)
    assert len(chunks) > 2
    assert all(c.text.startswith("| Name | Notes |") for c in chunks)


def test_fixed_chunking_overlaps():
    a = parse_article(refund_html(), REFUND_URL)
    chunks = chunk_article(a, "fixed", max_chars=300, overlap_chars=60)
    assert len(chunks) >= 3
    assert chunks[0].text[-20:].strip()[:10] in a.body_text
