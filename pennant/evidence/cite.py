"""Render one piece of evidence as a one-line citation an analyst can check.

Design choice: one fixed format per source, so every fact behind a vote can be
followed back by hand:
  notice   Tesco notice p.10: "Chair: Barratt Redrow plc and ICE Clear Europe"
  ch       Companies House: <URL> (updated 2026-04-02, checked 2026-10-26)
  web      Web: <title> <URL> — "<quote>" (updated 2026-10-20)
  analyst  Analyst (dec:3): "<reason>"
Fetched sources end with the date the answer last changed and, if a later fetch
confirmed it, the date of that check.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from pennant.core.config import notice_label
from pennant.core.models import Evidence, parse_notice_ref

__all__ = ["cite", "notice_label"]  # label: the company part of any notice's filename
MAX_QUOTE = 240


def _quoted(quote: str | None) -> str:
    if not quote:
        return ""
    q = " ".join(quote.split())
    if len(q) > MAX_QUOTE:
        q = q[: MAX_QUOTE - 1].rstrip() + "…"
    return f'"{q}"'


def _day(when: datetime) -> date:
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone().date()


def dated(evidence: Evidence) -> str:
    """' (updated D)', or ' (updated D, checked E)' when a later fetch confirmed it."""
    if evidence.fetched_at is None:
        return ""
    updated = _day(evidence.fetched_at)
    checked = _day(evidence.checked_at) if evidence.checked_at else updated
    return f" (updated {updated}" + (f", checked {checked})" if checked != updated else ")")


def cite(evidence: Evidence) -> str:
    q = _quoted(evidence.quote)
    match evidence.source:
        case "notice":
            doc, page = parse_notice_ref(evidence.ref)
            head = f"{notice_label(doc)} notice" + (f" p.{page}" if page else "")
            return f"{head}: {q}" if q else head
        case "ch":
            return f"Companies House: {evidence.ref}" + (f" — {q}" if q else "") + dated(evidence)
        case "web":
            parts = ["Web:", evidence.title, evidence.ref]
            head = " ".join(p for p in parts if p)
            return (f"{head} — {q}" if q else head) + dated(evidence)
        case "analyst":
            ref = f" ({evidence.ref})" if evidence.ref else ""
            return f"Analyst{ref}: {q}" if q else f"Analyst{ref}"
    raise ValueError(f"unknown evidence source: {evidence.source!r}")
