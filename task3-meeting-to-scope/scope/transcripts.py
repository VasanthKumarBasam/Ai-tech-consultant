"""Transcript parsing, chunking for long meetings, and quote verification."""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .models import SourceRef, TranscriptLine

LINE_RX = re.compile(r"^\[(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<speaker>[^:]{1,80}):\s*(?P<text>.+)$")
META_RX = re.compile(r"^#\s*(?P<key>[A-Za-z ]+):\s*(?P<value>.+)$")


@dataclass
class Transcript:
    meeting_id: str
    meta: dict[str, str]
    lines: list[TranscriptLine]

    @property
    def title(self) -> str:
        return self.meta.get("meeting", self.meeting_id)

    @property
    def date(self) -> str | None:
        return self.meta.get("date")

    def render(self, lines: list[TranscriptLine] | None = None) -> str:
        return "\n".join(f"L{l.n} [{l.timestamp or '--'}] {l.speaker or 'Unknown'}: {l.text}" for l in (lines or self.lines))

    def line(self, n: int) -> TranscriptLine | None:
        return next((l for l in self.lines if l.n == n), None)


def parse_transcript(meeting_id: str, text: str) -> Transcript:
    """Accepts '[hh:mm:ss] Speaker: text' lines plus optional '# Key: value' header lines.
    Lines without a timestamp are kept (speaker carried over) so pasted notes still work."""
    meta: dict[str, str] = {}
    lines: list[TranscriptLine] = []
    last_speaker = None
    for i, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s:
            continue
        m = META_RX.match(s)
        if m and not lines:
            meta[m.group("key").strip().lower()] = m.group("value").strip()
            continue
        m = LINE_RX.match(s)
        if m:
            last_speaker = m.group("speaker").strip()
            lines.append(TranscriptLine(n=i, timestamp=m.group("ts"), speaker=last_speaker, text=m.group("text").strip()))
        elif ":" in s[:40] and not s.startswith("http"):
            spk, _, rest = s.partition(":")
            last_speaker = spk.strip()
            lines.append(TranscriptLine(n=i, timestamp=None, speaker=last_speaker, text=rest.strip()))
        else:
            lines.append(TranscriptLine(n=i, timestamp=None, speaker=last_speaker, text=s))
    return Transcript(meeting_id, meta, lines)


def chunk_lines(t: Transcript, max_chars: int = 6000, overlap_lines: int = 3) -> list[list[TranscriptLine]]:
    """Split on line boundaries into windows of ~max_chars with a few lines of overlap,
    so a requirement discussed across a boundary is seen whole in at least one chunk."""
    chunks: list[list[TranscriptLine]] = []
    cur: list[TranscriptLine] = []
    size = 0
    for line in t.lines:
        ln = len(line.text) + 40
        if cur and size + ln > max_chars:
            chunks.append(cur)
            cur = cur[-overlap_lines:] if overlap_lines else []
            size = sum(len(x.text) + 40 for x in cur)
        cur.append(line)
        size += ln
    if cur:
        chunks.append(cur)
    return chunks


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


def verify_quote(t: Transcript, line_no: int, quote: str, window: int = 4) -> SourceRef:
    """Locate the quote at the cited line (or within +-window lines). If it can't be found
    the reference is kept but marked unverified, so a reviewer knows to check it."""
    q = _norm(quote)
    best: tuple[float, TranscriptLine | None] = (0.0, None)
    for line in t.lines:
        if abs(line.n - line_no) > window:
            continue
        txt = _norm(line.text)
        if q and q in txt:
            score = 1.0
        else:
            score = SequenceMatcher(None, q, txt).find_longest_match(0, len(q), 0, len(txt)).size / max(1, len(q))
        if line.n == line_no:
            score += 0.05
        if score > best[0]:
            best = (score, line)
    score, line = best
    if line and score >= 0.6:
        return SourceRef(meeting_id=t.meeting_id, line=line.n, timestamp=line.timestamp, speaker=line.speaker, quote=quote, verified=True)
    cited = t.line(line_no)
    return SourceRef(meeting_id=t.meeting_id, line=line_no, timestamp=cited.timestamp if cited else None,
                     speaker=cited.speaker if cited else None, quote=quote, verified=False)


def client_speakers(t: Transcript) -> set[str]:
    """Speakers marked '(Client ...)' in the participants header."""
    raw = t.meta.get("participants", "")
    out = set()
    for part in re.split(r",\s*(?![^()]*\))", raw):
        m = re.match(r"\s*([^()]+?)\s*\(([^)]*)\)", part)
        if m and "client" in m.group(2).lower():
            out.add(m.group(1).strip())
    return out
