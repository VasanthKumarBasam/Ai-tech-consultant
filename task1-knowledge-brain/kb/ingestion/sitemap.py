"""Sitemap download + parsing (handles <urlset> and nested <sitemapindex>)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urldefrag, urlparse


@dataclass(frozen=True)
class SitemapEntry:
    url: str
    lastmod: str | None


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def normalize_url(url: str) -> str | None:
    url = (url or "").strip()
    url, _ = urldefrag(url)
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.netloc:
        return None
    return url


def parse_sitemap(xml_text: str) -> tuple[list[SitemapEntry], list[str]]:
    """Return (url entries, child sitemap URLs)."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    kind = _strip_ns(root.tag)
    entries: list[SitemapEntry] = []
    children: list[str] = []
    for node in root:
        name = _strip_ns(node.tag)
        loc = lastmod = None
        for child in node:
            cname = _strip_ns(child.tag)
            if cname == "loc":
                loc = (child.text or "").strip()
            elif cname == "lastmod":
                lastmod = (child.text or "").strip() or None
        if not loc:
            continue
        if kind == "sitemapindex" and name == "sitemap":
            children.append(loc)
        elif name == "url":
            norm = normalize_url(loc)
            if norm:
                entries.append(SitemapEntry(norm, lastmod))
    return entries, children


def filter_articles(entries: list[SitemapEntry], pattern: str) -> list[SitemapEntry]:
    """Keep article pages, dedupe by URL (last lastmod wins), stable order."""
    rx = re.compile(pattern)
    seen: dict[str, SitemapEntry] = {}
    for e in entries:
        if rx.search(e.url):
            seen[e.url] = e
    return list(seen.values())


def article_id_from_url(url: str) -> str | None:
    m = re.search(r"/articles/(\d+)", url)
    return m.group(1) if m else None
