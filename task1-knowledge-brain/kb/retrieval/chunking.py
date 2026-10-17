"""Chunking strategies.

`heading` (default): one chunk per article section; small sections are merged
forward, long ones are split on block boundaries with one block of overlap.
Lists stay with the sentence that introduces them, tables keep their header
row on every piece. Every chunk is prefixed with "Title > Section" so the
embedding and the keyword index both see where the text came from.

`fixed`: sliding character window, kept for the chunking comparison in EVALS.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..ingestion.parser import Article, Block, Section


@dataclass
class Chunk:
    index: int
    section: str
    text: str                      # text stored + shown as evidence
    image_refs: list[str] = field(default_factory=list)

    def embed_text(self, title: str) -> str:
        return f"{title} > {self.section}\n{self.text}"


def _block_text(b: Block) -> str:
    return f"[Screenshot {b.image_ref}]" if b.type == "image" else b.text


def _split_table(text: str, max_chars: int) -> list[str]:
    lines = text.split("\n")
    header = lines[:2] if len(lines) > 1 and lines[1].startswith("|") and "---" in lines[1] else lines[:1]
    body = lines[len(header):]
    pieces, cur = [], list(header)
    for row in body:
        if sum(len(x) + 1 for x in cur) + len(row) > max_chars and len(cur) > len(header):
            pieces.append("\n".join(cur))
            cur = list(header)
        cur.append(row)
    pieces.append("\n".join(cur))
    return pieces


def _units(section: Section, max_chars: int) -> list[list[Block]]:
    """Group blocks into indivisible units: an intro sentence ending in ':' stays with its list."""
    units: list[list[Block]] = []
    for b in section.blocks:
        if b.type == "table" and len(b.text) > max_chars:
            for piece in _split_table(b.text, max_chars):
                units.append([Block("table", piece)])
            continue
        if units and b.type in {"list", "image"} and units[-1][-1].type == "text" and units[-1][-1].text.rstrip().endswith(":"):
            units[-1].append(b)
        elif units and b.type == "image":
            units[-1].append(b)  # screenshots stay with the step they illustrate
        else:
            units.append([b])
    return units


def chunk_heading(article: Article, max_chars: int = 1800, min_chars: int = 250, overlap_units: int = 1) -> list[Chunk]:
    raw: list[tuple[str, list[Block]]] = []
    for s in article.sections:
        units = _units(s, max_chars)
        cur: list[list[Block]] = []
        size = 0
        for u in units:
            usize = sum(len(_block_text(b)) + 1 for b in u)
            if cur and size + usize > max_chars:
                raw.append((s.heading, [b for x in cur for b in x]))
                cur = cur[-overlap_units:] if overlap_units else []
                size = sum(len(_block_text(b)) + 1 for x in cur for b in x)
                if size + usize > max_chars:  # overlap alone would overflow
                    cur, size = [], 0
            cur.append(u)
            size += usize
        if cur:
            raw.append((s.heading, [b for x in cur for b in x]))

    # merge tiny pieces into the following piece (keeps headings like "Notes" useful)
    merged: list[tuple[str, list[Block]]] = []
    carry: tuple[str, list[Block]] | None = None
    for heading, blocks in raw:
        if carry:
            heading = f"{carry[0]} / {heading}" if carry[0] != heading else heading
            blocks = carry[1] + blocks
            carry = None
        size = sum(len(_block_text(b)) for b in blocks)
        if size < min_chars:
            carry = (heading, blocks)
        else:
            merged.append((heading, blocks))
    if carry:
        if merged and sum(len(_block_text(b)) for b in merged[-1][1]) + sum(len(_block_text(b)) for b in carry[1]) <= max_chars * 1.3:
            h, b = merged.pop()
            merged.append((h if h == carry[0] else f"{h} / {carry[0]}", b + carry[1]))
        else:
            merged.append(carry)

    chunks = []
    for i, (heading, blocks) in enumerate(merged):
        text = "\n".join(_block_text(b) for b in blocks if _block_text(b)).strip()
        if not text:
            continue
        refs = [b.image_ref for b in blocks if b.type == "image" and b.image_ref]
        chunks.append(Chunk(index=len(chunks), section=heading or article.title, text=text, image_refs=refs))
    return chunks


def chunk_fixed(article: Article, size: int = 1000, overlap: int = 150) -> list[Chunk]:
    text = article.body_text
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):  # prefer to cut at a newline / sentence end
            cut = max(text.rfind("\n", start + size // 2, end), text.rfind(". ", start + size // 2, end))
            if cut > start:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            refs = [r for r in (i.ref for i in article.images) if f"[Screenshot {r}]" in piece]
            chunks.append(Chunk(index=len(chunks), section=article.title, text=piece, image_refs=refs))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def chunk_article(article: Article, strategy: str = "heading", max_chars: int = 1800, min_chars: int = 250, overlap_chars: int = 200) -> list[Chunk]:
    if strategy == "fixed":
        return chunk_fixed(article, size=max_chars, overlap=overlap_chars)
    return chunk_heading(article, max_chars=max_chars, min_chars=min_chars)
