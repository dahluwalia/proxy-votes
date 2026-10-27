"""Check that a quote really is on the page it was cited from.

Design choice: grounding is three cheap string tests in order of strictness:
`exact` substring, then `normalized` (NFKC, straight quotes, one dash, zero-width
and private-use characters removed, line-break hyphens joined, whitespace collapsed,
case-folded), then rapidfuzz
`partial_ratio >= 95` as `fuzzy`. The cited page is tried first, then the pages
either side, because citations can be off by one. A quote that passes none of
these is ungrounded, so a hallucinated appointment never reaches a vote.

Page text comes from PyMuPDF. `python -m pennant.evidence.grounding` rewrites the
committed fixtures in tests/fixtures/pages/<pdf stem>.json ({"doc", "pages"}).
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

import pymupdf
from rapidfuzz import fuzz

from pennant import logs
from pennant.core.config import FIXTURES_DIR, NOTICES_DIR, meeting_key_for, notices

PAGES_FIXTURES = FIXTURES_DIR / "pages"
FUZZY_CUTOFF = 95

_QUOTES = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-", "­": None,
})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # e.g. \x07 in bullets
_LINEBREAK_HYPHEN = re.compile(r"(\w)-[ \t]*\r?\n\s*(\w)")
_INWORD_HYPHEN = re.compile(r"(?<=\w)-(?=\w)")
_SPACE = re.compile(r"\s+")


def _category(cat: str) -> re.Pattern[str]:
    """A character class of every code point in one Unicode general category."""
    cps = [c for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == cat]
    ranges, start = [], cps[0]
    for prev, cur in zip(cps, [*cps[1:], None], strict=True):
        if cur != prev + 1:
            ranges.append(re.escape(chr(start)) + (f"-{re.escape(chr(prev))}" if prev > start
                                                   else ""))
            start = cur
    return re.compile(f"[{''.join(ranges)}]")


_FORMAT = _category("Cf")  # zero-width space and joiners, BOM, soft hyphen: dropped
_PRIVATE = _category("Co")  # private-use glyphs, e.g. a font's bullet: a separator


# --- page text ---------------------------------------------------------------


def page_texts(pdf_path: str | Path) -> list[str]:
    """Text of each page in reading order; index 0 is page 1."""
    with pymupdf.open(str(pdf_path)) as doc:
        pages = [page.get_text("text") for page in doc]
    logs.count("pdf.pages_read", len(pages))
    return pages


def load_pages(doc: str, fixtures: Path = PAGES_FIXTURES) -> list[str]:
    """Committed page-text fixture for a notice, by PDF filename or meeting key."""
    path = fixtures / f"{Path(doc).stem}.json"
    if not path.exists():  # a meeting key: find the fixture whose filename gives that key
        path = next((p for p in sorted(fixtures.glob("*.json"))
                     if meeting_key_for(p.stem) == doc), path)
    return json.loads(path.read_text())["pages"]


def write_page_fixtures(out_dir: Path = PAGES_FIXTURES,
                        notices_dir: Path = NOTICES_DIR) -> list[Path]:
    """One fixture per PDF in the notices folder, whatever notices it holds."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in sorted(notices(notices_dir).values()):
        path = out_dir / f"{Path(name).stem}.json"
        payload = {"doc": name, "pages": page_texts(notices_dir / name)}
        path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
        written.append(path)
    return written


# --- grounding ---------------------------------------------------------------


def normalize(text: str) -> str:
    """NFKC, straight quotes and dashes, invisible format characters dropped, control and
    private-use characters to spaces, line-break and in-word hyphens joined, whitespace
    collapsed, case-folded. Applied to both page and quote."""
    s = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    s = _PRIVATE.sub(" ", _CONTROL.sub(" ", _FORMAT.sub("", s)))
    s = _LINEBREAK_HYPHEN.sub(r"\1\2", s)  # "multi-\nnational" -> "multinational"
    s = _INWORD_HYPHEN.sub("", s)  # so "non-executive" == "non-\nexecutive"
    return _SPACE.sub(" ", s).strip().casefold()


def ground(page_text: str, quote: str) -> tuple[bool, str | None]:
    """Is `quote` on this page? Returns (found, method); method is None when not found."""
    if not quote or not quote.strip() or not page_text:
        return False, None
    if quote in page_text:
        return True, "exact"
    q, page = normalize(quote), normalize(page_text)
    if not q:
        return False, None
    if q in page:
        return True, "normalized"
    if fuzz.partial_ratio(q, page, score_cutoff=FUZZY_CUTOFF):
        return True, "fuzzy"
    return False, None


def ground_near(
    pages: list[str], page: int | None, quote: str
) -> tuple[bool, str | None, int | None]:
    """Ground on the cited 1-based `page`, then page-1 and page+1 (all pages when the
    citation has no page). Returns (found, method, 1-based page it was found on)."""
    if page is None:
        order = list(range(1, len(pages) + 1))
    else:
        order = [p for p in (page, page - 1, page + 1) if 1 <= p <= len(pages)]
    for p in order:
        found, method = ground(pages[p - 1], quote)
        if found:
            return True, method, p
    return False, None, None


if __name__ == "__main__":
    for written in write_page_fixtures():
        print(written)
