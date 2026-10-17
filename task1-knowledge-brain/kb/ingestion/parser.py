"""Help-centre article extraction.

Turns a Freshdesk (Brilliant Directories Solution Center) page into a
normalised article: title, ordered sections of typed blocks (text / list /
table / image / code), image references with their surrounding text, and a
content hash used for change detection and idempotent re-ingestion.

Navigation, headers, footers, vote widgets, related-article lists and tracking
pixels are stripped so they never pollute retrieval.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, NavigableString, Tag

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCKS = {"p", "div", "section", "article", "blockquote", "figure", "figcaption", "header", "aside", "dl", "dd", "dt", "center"}
STRUCTURAL = HEADINGS | {"ul", "ol", "table", "img", "pre"} | BLOCKS
NOISE_SELECTORS = [
    "script", "style", "noscript", "iframe", "form", "button", "nav", "footer", "svg",
    "[class*=vote]", "[class*=related]", "[class*=breadcrumb]", "[id*=related]",
    "[class*=article-meta]", "[class*=feedback]", "[class*=attachments]",
]
BODY_SELECTORS = ["#article-body", ".article-body", "[itemprop=articleBody]", ".fr-view", "article", "main"]
TITLE_SELECTORS = ["h1.article-title", ".article-title h1", ".article-title", ".article-view h2.heading", "h2.heading", "h1.heading"]
SITE_SUFFIXES = re.compile(r"\s*[|:]\s*(Brilliant Directories.*|Solution Center.*|Support.*)$", re.I)


@dataclass
class ImageRef:
    ref: str            # IMG-1, IMG-2 ... (stable within an article version)
    url: str
    key: str            # URL without query string -> stable cache/hash key
    alt: str
    section: str
    context_before: str = ""
    context_after: str = ""


@dataclass
class Block:
    type: str           # text | list | table | image | code
    text: str = ""
    image_ref: str | None = None


@dataclass
class Section:
    heading: str
    level: int
    blocks: list[Block] = field(default_factory=list)

    @property
    def text(self) -> str:
        parts = []
        for b in self.blocks:
            parts.append(f"[Screenshot {b.image_ref}]" if b.type == "image" else b.text)
        return "\n".join(p for p in parts if p)


@dataclass
class Article:
    url: str
    article_id: str | None
    title: str
    sections: list[Section]
    images: list[ImageRef]
    modified_on_text: str | None
    content_hash: str

    @property
    def body_text(self) -> str:
        out = []
        for s in self.sections:
            if s.heading and s.heading != self.title:
                out.append(f"## {s.heading}")
            out.append(s.text)
        return "\n".join(o for o in out if o).strip()

    def sections_json(self) -> str:
        return json.dumps([asdict(s) for s in self.sections])

    def images_json(self) -> str:
        return json.dumps([asdict(i) for i in self.images])


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def image_key(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def _img_src(tag: Tag) -> str | None:
    for attr in ("src", "data-src", "data-original"):
        v = tag.get(attr)
        if v and not v.startswith("data:"):
            return v
    srcset = tag.get("srcset")
    if srcset:
        return srcset.split(",")[0].strip().split(" ")[0]
    return None


def _is_content_image(url: str, tag: Tag) -> bool:
    low = url.lower()
    if low.endswith(".svg") or "emoji" in low or low.rstrip("/").endswith("/hit") or "logo" in low.split("/")[-1]:
        return False
    for dim in ("width", "height"):
        v = str(tag.get(dim, "")).replace("px", "")
        if v.isdigit() and int(v) < 60:
            return False
    return True


class _Walker:
    def __init__(self, base_url: str, title: str):
        self.base_url = base_url
        self.sections: list[Section] = [Section(heading=title, level=1)]
        self.images: list[ImageRef] = []
        self.buf: list[str] = []
        self._seen_images: set[str] = set()

    @property
    def cur(self) -> Section:
        return self.sections[-1]

    def flush(self) -> None:
        text = _clean("".join(self.buf))
        self.buf = []
        if text:
            self.cur.blocks.append(Block("text", text))

    def add_image(self, tag: Tag) -> None:
        src = _img_src(tag)
        if not src:
            return
        url = urljoin(self.base_url, src)
        if not _is_content_image(url, tag):
            return
        key = image_key(url)
        if key in self._seen_images:
            return
        self._seen_images.add(key)
        self.flush()
        ref = f"IMG-{len(self.images) + 1}"
        alt = _clean(tag.get("alt") or tag.get("title") or "")
        self.images.append(ImageRef(ref=ref, url=url, key=key, alt=alt, section=self.cur.heading))
        self.cur.blocks.append(Block("image", alt, image_ref=ref))

    def list_text(self, tag: Tag, depth: int = 0) -> str:
        lines = []
        ordered = tag.name == "ol"
        for i, li in enumerate(tag.find_all("li", recursive=False), 1):
            nested = li.find_all(["ul", "ol"], recursive=False)
            for n in nested:
                n.extract()
            for img in li.find_all("img"):
                self.add_image(img)
                img.decompose()
            marker = f"{i}." if ordered else "-"
            item = _clean(li.get_text(" "))
            if item:
                lines.append(f"{'  ' * depth}{marker} {item}")
            for n in nested:
                sub = self.list_text(n, depth + 1)
                if sub:
                    lines.append(sub)
        return "\n".join(lines)

    def table_text(self, tag: Tag) -> str:
        rows = []
        for tr in tag.find_all("tr"):
            cells = [_clean(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append("| " + " | ".join(cells) + " |")
        if len(rows) > 1:
            ncols = rows[0].count("|") - 1
            rows.insert(1, "|" + " --- |" * ncols)
        for img in tag.find_all("img"):
            self.add_image(img)
        return "\n".join(rows)

    def walk(self, node: Tag) -> None:
        for child in list(node.children):
            if isinstance(child, NavigableString):
                if child.__class__.__name__ in {"Comment", "Doctype", "CData", "ProcessingInstruction"}:
                    continue
                self.buf.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name in HEADINGS:
                self.flush()
                heading = _clean(child.get_text(" "))
                for img in child.find_all("img"):
                    self.add_image(img)
                if heading:
                    self.sections.append(Section(heading=heading, level=int(name[1])))
            elif name in {"ul", "ol"}:
                self.flush()
                text = self.list_text(child)
                if text:
                    self.cur.blocks.append(Block("list", text))
            elif name == "table":
                self.flush()
                text = self.table_text(child)
                if text:
                    self.cur.blocks.append(Block("table", text))
            elif name == "pre":
                self.flush()
                self.cur.blocks.append(Block("code", child.get_text().strip()))
            elif name == "img":
                self.add_image(child)
            elif name == "br":
                self.buf.append("\n")
            elif name in BLOCKS or name == "li":
                self.flush()
                self.walk(child)
                self.flush()
            elif child.find(list(STRUCTURAL)):
                self.walk(child)  # inline wrapper (a/span/strong) around images or blocks
            else:
                self.buf.append(child.get_text())
        # no flush here: inline text continues until the enclosing block ends

    def finish(self) -> tuple[list[Section], list[ImageRef]]:
        self.flush()
        sections = [s for s in self.sections if s.blocks]
        # attach surrounding text to each image (same section, nearest blocks)
        for s in sections:
            for idx, b in enumerate(s.blocks):
                if b.type != "image":
                    continue
                img = next(i for i in self.images if i.ref == b.image_ref)
                before = [x.text for x in s.blocks[:idx] if x.type != "image"]
                after = [x.text for x in s.blocks[idx + 1 :] if x.type != "image"]
                img.context_before = " ".join(before)[-400:]
                img.context_after = " ".join(after)[:250]
                img.section = s.heading
        return sections, self.images


def _pick(soup: BeautifulSoup, selectors: list[str]) -> Tag | None:
    for sel in selectors:
        el = soup.select_one(sel)
        if el and _clean(el.get_text(" ")):
            return el
    return None


def _largest_text_block(soup: BeautifulSoup) -> Tag | None:
    best, best_len = None, 0
    for div in soup.find_all(["div", "section"]):
        n = sum(len(p.get_text()) for p in div.find_all("p", recursive=False))
        if n > best_len:
            best, best_len = div, n
    return best


def extract_title(soup: BeautifulSoup) -> str:
    """Article-specific selectors first, then <title>/og:title (minus the site suffix), then any <h1>."""
    el = _pick(soup, TITLE_SELECTORS)
    if el:
        return _clean(el.get_text(" "))
    og = soup.find("meta", attrs={"property": "og:title"})
    for raw in (og.get("content") if og else None, soup.title.string if soup.title else None):
        if raw:
            t = SITE_SUFFIXES.sub("", _clean(raw)).strip()
            if t:
                return t
    h1 = soup.find("h1")
    return _clean(h1.get_text(" ")) if h1 else "Untitled"


def parse_article(html: str, url: str, article_id: str | None = None) -> Article:
    soup = BeautifulSoup(html, "lxml")
    title = extract_title(soup)
    page_text = soup.get_text("\n")
    m = re.search(r"Modified on[:\s]+([^\n]{5,60})", page_text)
    modified_on = _clean(m.group(1)) if m else None

    body = _pick(soup, BODY_SELECTORS) or _largest_text_block(soup) or soup.body or soup
    for sel in NOISE_SELECTORS:
        for el in body.select(sel):
            el.decompose()
    # the title heading often sits inside the body container; don't duplicate it
    for h in body.find_all(list(HEADINGS)):
        if _clean(h.get_text(" ")) == title:
            h.decompose()

    w = _Walker(url, title)
    w.walk(body)
    sections, images = w.finish()

    hasher = hashlib.sha256()
    hasher.update(title.encode())
    for s in sections:
        hasher.update(s.heading.encode())
        for b in s.blocks:
            hasher.update(b.type.encode())
            hasher.update(b.text.encode())
    for i in images:
        hasher.update(i.key.encode())
    return Article(
        url=url,
        article_id=article_id,
        title=title,
        sections=sections,
        images=images,
        modified_on_text=modified_on,
        content_hash=hasher.hexdigest(),
    )
