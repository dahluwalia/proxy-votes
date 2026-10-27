"""M1.2: check each notice's extract against the PDF, then record meeting, roster and seats.

Design choice: the extraction model proposes and the PDF text decides. The meeting facts
are confirmed in extract/meeting.py, and every director's name must be in the document;
a notice that fails either is a stage error, so gate G1 holds the run. Every
appointment's quote must be on its cited page (or a page either side), and its
organisation must be in the quote and on that page, exactly or normalized (never
fuzzily, so "The Bank Group" can't pass for "The Rank Group"); one that isn't is dropped
and becomes a review item, so a hallucinated seat never reaches a vote. Role facts come
from the M5.1 rules table, and `current` from the tense. A director's stated role must be
printed, as whole words, just after their name (cited pages ±1 first); otherwise it is
treated as unstated and reviewed. Each notice is checked on its own: an exception in one
becomes that notice's stage error; the others are still recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pennant import logs
from pennant.classify.research import org_key
from pennant.classify.roles import classify_role
from pennant.core import db
from pennant.core.config import NOTICES_DIR, Policy, notices
from pennant.core.models import (
    Evidence,
    Meeting,
    ReviewItem,
    RoleType,
    RosterEntry,
    RunContext,
    Seat,
    StageReport,
    mention_id,
    notice_ref,
)
from pennant.core.tri import NO, UNKNOWN, YES
from pennant.evidence.grounding import ground_near, normalize, page_texts
from pennant.extract import backends
from pennant.extract.meeting import check_meeting, gap_problem
from pennant.extract.schema import ExtractedAppointment, ExtractedDirector, NoticeExtract

ROLE_WINDOW = 150  # normalized chars after a director's name within which their role starts

log = logs.get("extract")


@dataclass
class NoticeResult:
    meeting: Meeting | None = None
    roster: list[RosterEntry] = field(default_factory=list)
    seats: list[Seat] = field(default_factory=list)
    reviews: list[ReviewItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --- grounding ---------------------------------------------------------------


def find_text(pages: list[str], cited: list[int], text: str) -> int | None:
    """1-based page where `text` is grounded: cited pages (±1) first, then anywhere."""
    for page in [*cited, None]:
        found, _, on = ground_near(pages, page, text)
        if found:
            return on
    return None


def phrase(text: str) -> re.Pattern[str]:
    """`text` as whole words in normalized text (a plural s allowed), so "Executive
    Director" is not found inside "Non-executive Director"."""
    return re.compile(r"(?<![\w-])" + re.escape(normalize(text)) + r"s?(?![\w-])")


def _near(pages: list[str], centres: list[int]) -> list[int]:
    """Each centre page then the pages either side, in citation order, without repeats."""
    near = (p for c in centres for p in (c, c - 1, c + 1) if 1 <= p <= len(pages))
    return list(dict.fromkeys(near))


def find_role(pages: list[str], cited: list[int], name: str, role: str) -> int | None:
    """1-based page where `role` starts within ROLE_WINDOW chars after `name` (a bio
    heading can run onto the next page): cited pages ±1 first, then any other page that
    names them ("X and Y joined the Board as Non-Executive Directors"). None if nowhere,
    so a role printed only for someone else on a nearby page doesn't count."""
    name_re, role_re = phrase(name), phrase(role)
    near = _near(pages, cited)
    for p in near + [p for p in range(1, len(pages) + 1) if p not in near]:
        here = normalize(pages[p - 1])
        text = normalize(pages[p - 1] + "\n" + (pages[p] if p < len(pages) else ""))
        for m in name_re.finditer(text, 0, len(here)):
            r = role_re.search(text, m.end())
            if r and r.start() - m.end() <= ROLE_WINDOW:
                return p if r.start() < len(here) else p + 1
    return None


def ground_appointment(a: ExtractedAppointment, pages: list[str]) -> tuple[int | None, str]:
    """(page the quote is on, method), or (None, why it is ungrounded)."""
    if not a.quote or not a.organisation:
        return None, "no quote or organisation"
    org = phrase(a.organisation)
    for cited in a.cited_pages() or [None]:
        found, method, page = ground_near(pages, cited, a.quote)
        if found and page:
            if not org.search(normalize(a.quote)):
                return None, f"the quote does not name {a.organisation!r}"
            if not any(org.search(normalize(pages[p - 1])) for p in _near(pages, [page])):
                return None, f"{a.organisation!r} is not on p.{page} (or the pages either side)"
            return page, method or ""
    where = ", ".join(f"p.{p}" for p in a.cited_pages()) or "any page"
    return None, f"quote not found on {where} (or the pages either side)"


# --- records -----------------------------------------------------------------


def _roster_and_issuer_seat(
    d: ExtractedDirector, mtg: Meeting, doc: str, name_page: int, role_page: int | None,
    policy: Policy,
) -> tuple[RosterEntry, Seat]:
    mid = mention_id(mtg.company_number, d.resolution_no or 0)
    role = d.role_at_company if role_page else None
    evidence = [Evidence(source="notice", ref=notice_ref(doc, name_page), quote=d.name)]
    if role and role_page:
        evidence.append(Evidence(source="notice", ref=notice_ref(doc, role_page), quote=role))
    rf = classify_role(role, "current", chair_designate=policy.chair_designate_counts_as_chair)
    entry = RosterEntry(
        mention_id=mid, meeting_id=mtg.meeting_id, resolution_no=d.resolution_no or 0,
        name=d.name, role_text_at_issuer=role, role_type_at_issuer=rf.role_type if role else None,
        coverage=d.coverage, evidence=evidence)
    seat = Seat(
        seat_id=f"seat:{mtg.company_number}:r{d.resolution_no}:0", mention_id=mid,
        org_name=mtg.company_name, org_key=org_key(mtg.company_name),
        ch_company_number=mtg.company_number, role_text=role,
        role_type=rf.role_type if role else RoleType.other, board_seat=YES,
        chair=rf.chair if role else UNKNOWN, executive=rf.executive if role else UNKNOWN,
        current=YES, identity=YES, evidence=evidence)
    return entry, seat


_GENERIC = (RoleType.other, RoleType.director_unspecified)
# Whole phrases in an organisation's name that make it a trustee or advisory body.
_BODY_NAMES = ((RoleType.trustee, re.compile(r"\bboard of trustees\b|\btrustee board\b")),
               (RoleType.adviser, re.compile(r"\badvisory (?:board|council|panel)s?\b")))


def appointment_facts(a: ExtractedAppointment, policy: Policy):
    """Rules on the role text. The organisation can only rule a seat out, and only for a
    generic role (other or bare director) or a chair: "Other: International Advisory Board
    of Adobe Inc" is an adviser, and "Chair: the Almeida Theatre Board of Trustees" chairs a
    trustee board, so it is a trustee. An explicit NED or executive role is never overridden
    by words in the organisation's name ("Mentor Graphics Corporation")."""
    designate = policy.chair_designate_counts_as_chair
    rf = classify_role(a.role, a.tense, chair_designate=designate)
    if rf.role_type not in (*_GENERIC, RoleType.chair, RoleType.deputy_chair):
        return rf
    body = next((t for t, pattern in _BODY_NAMES if pattern.search(normalize(a.organisation))),
                None)
    return rf._replace(role_type=body, board_seat=NO, chair=NO, executive=NO) if body else rf


def _appointment_seat(
    a: ExtractedAppointment, i: int, entry: RosterEntry, mtg: Meeting, doc: str, page: int,
    policy: Policy,
) -> Seat:
    rf = appointment_facts(a, policy)
    return Seat(
        seat_id=f"seat:{mtg.company_number}:r{entry.resolution_no}:{i}",
        mention_id=entry.mention_id, org_name=a.organisation, org_key=org_key(a.organisation),
        role_text=a.role, role_type=rf.role_type, board_seat=rf.board_seat, chair=rf.chair,
        executive=rf.executive, current=rf.current, identity=YES,
        evidence=[Evidence(source="notice", ref=notice_ref(doc, page), quote=a.quote)])


def check_notice(
    ex: NoticeExtract, pages: list[str], policy: Policy, key: str | None = None
) -> NoticeResult:
    """Validate one notice's extract against its page text and build its records."""
    res = NoticeResult()
    res.reviews = [ReviewItem(kind="extract_warning", subject=ex.doc, detail=w)
                   for w in ex.warnings]
    mtg, errors = check_meeting(ex, pages, key)
    gap = gap_problem([d.resolution_no for d in ex.directors])
    res.errors = errors + ([f"{ex.doc}: {gap}"] if gap else [])
    if mtg is None or gap:
        return res
    for d in sorted(ex.directors, key=lambda d: d.resolution_no or 0):
        cited = d.cited_pages()
        name_page = find_text(pages, cited, d.name)
        if name_page is None:
            res.errors.append(f"{ex.doc}: director {d.name!r} (resolution {d.resolution_no}) "
                              "is not named in the PDF")
            continue
        role_page = (find_role(pages, [*d.cited_pages("role_at_company"), name_page], d.name,
                               d.role_at_company) if d.role_at_company else None)
        entry, seat = _roster_and_issuer_seat(d, mtg, ex.doc, name_page, role_page, policy)
        if d.role_at_company and role_page is None:
            res.reviews.append(ReviewItem(
                kind="ungrounded_role", subject=entry.mention_id,
                detail=f"role {d.role_at_company!r} is not in {ex.doc}; treated as not stated",
                suggested_command=f'pennant show "{d.name}"'))
        res.roster.append(entry)
        res.seats.append(seat)
        for i, a in enumerate(d.appointments, 1):
            page, how = ground_appointment(a, pages)
            if page is None:
                res.reviews.append(ReviewItem(
                    kind="ungrounded_quote", subject=entry.mention_id,
                    detail=f"{a.organisation} ({a.role}) dropped: {how}: {a.quote!r}",
                    suggested_command=f'pennant show "{d.name}"'))
                continue
            res.seats.append(_appointment_seat(a, i, entry, mtg, ex.doc, page, policy))
    res.meeting = mtg
    return res


def extract_notice(pdf: Path, ctx: RunContext) -> NoticeExtract | None:
    """The notice's extract from the model `EXTRACT_MODEL` names (extract/backends.py)."""
    return backends.backend(ctx.settings).extract_notice(pdf, ctx)


def run(ctx: RunContext, notices_dir: Path = NOTICES_DIR) -> StageReport:
    """Extract and validate every notice; write meeting, roster, seat and review outputs."""
    results, errors = [], []
    for key, name in sorted(notices(notices_dir).items(), key=lambda kv: kv[1]):
        pdf = notices_dir / name
        try:
            ex = extract_notice(pdf, ctx)
            if ex is None:
                errors.append(f"{pdf.name}: no extract (offline and nothing recorded)")
                continue
            res = check_notice(ex, page_texts(pdf), ctx.policy, key)
        except Exception as exc:  # budget guard, failed job, unreadable PDF, a bug: fail closed
            log.exception("%s: extraction or validation failed", pdf.name)
            errors.append(f"{pdf.name}: extraction or validation failed: "
                          f"{type(exc).__name__}: {exc}")
            continue
        errors += res.errors
        results.append(res)
    reviews = [r for res in results for r in res.reviews]
    reviews += [ReviewItem(kind="extract_error", subject="extract", detail=e) for e in errors]
    outputs = {
        "meeting": [res.meeting for res in results if res.meeting],
        "roster": [e for res in results if res.meeting for e in res.roster],
        "seat": [s for res in results if res.meeting for s in res.seats],
        "review": reviews,
    }
    for kind, models in outputs.items():
        db.put_outputs(ctx.conn, ctx.run_id, kind, models)
    counts = {kind: len(models) for kind, models in outputs.items()}
    counts["ungrounded"] = sum(r.kind == "ungrounded_quote" for r in reviews)
    return StageReport(stage="extract", ok=not errors, counts=counts, errors=errors)
