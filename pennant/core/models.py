"""Every record that crosses a module boundary, plus the stage interface.

Design choice: plain pydantic v2 models that round-trip through JSON, so each run
can store them in `run_outputs` and any two runs can be diffed field by field.
Facts are TriField ("yes" / "no" / "unknown", default "unknown"): a fact nobody
has evidenced stays unknown. Ids are readable strings built by the helpers below.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pennant.core.config import Policy, Settings
from pennant.core.tri import TriField


class RoleType(StrEnum):
    executive_director = "executive_director"
    executive_chair = "executive_chair"
    chair = "chair"
    deputy_chair = "deputy_chair"
    non_executive_director = "non_executive_director"
    director_unspecified = "director_unspecified"
    officer_title = "officer_title"  # CEO, CFO, EVP, MD: an executive job, board seat unknown
    committee_chair = "committee_chair"
    adviser = "adviser"
    trustee = "trustee"
    other = "other"


class SeatStatus(StrEnum):
    confirmed = "confirmed"
    stale = "stale"
    contradiction = "contradiction"
    undisclosed = "undisclosed"
    ch_invisible = "ch_invisible"
    ch_unavailable = "ch_unavailable"
    past = "past"


class Vote(StrEnum):
    FOR = "FOR"
    AGAINST = "AGAINST"
    REVIEW = "REVIEW"


EvidenceSource = Literal["notice", "ch", "web", "analyst"]
Coverage = Literal["structured", "prose", "none"]
DecisionKind = Literal["must_link", "cannot_link", "set_fact", "revoke"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- ids ---------------------------------------------------------------------

_CO_NUM = re.compile(r"^([A-Z]{0,2})(\d+)$")


def pad_company_number(n: str | int) -> str:
    """Zero-pad a CH company number to 8 chars, keeping any prefix: 4967001 -> 04967001,
    SC12345 -> SC012345. Raises ValueError for anything that isn't a company number."""
    s = str(n).strip().upper().replace(" ", "")
    m = _CO_NUM.match(s)
    if not m or len(s) > 8:
        raise ValueError(f"not a company number: {n!r}")
    prefix, digits = m.groups()
    return prefix + digits.zfill(8 - len(prefix))


def meeting_id(company_number: str, agm_date: date | str) -> str:
    return f"mtg:{pad_company_number(company_number)}:{agm_date}"


def mention_id(company_number: str, resolution_no: int) -> str:
    return f"nm:{pad_company_number(company_number)}:r{resolution_no}"


def person_id(mention_ids: list[str]) -> str:
    """`p:` plus the smallest notice mention id in the cluster."""
    return f"p:{min(mention_ids)}"


def notice_ref(doc: str, page: int) -> str:
    """Evidence ref for a notice: '<pdf filename>#page=<1-based page>'."""
    return f"{doc}#page={page}"


def parse_notice_ref(ref: str) -> tuple[str, int | None]:
    doc, _, page = ref.partition("#page=")
    return doc, int(page) if page.isdigit() else None


# --- records -----------------------------------------------------------------


class Evidence(_Model):
    source: EvidenceSource
    ref: str  # notice_ref(doc, page) for notices; the URL for ch and web
    quote: str | None = None
    fetched_at: datetime | None = None  # last updated: when this answer first arrived
    title: str | None = None  # web page title, for cite()
    checked_at: datetime | None = None  # last fetch that returned the same answer


class Meeting(_Model):
    meeting_id: str
    company_name: str
    company_number: str = Field(pattern=r"^\d{8}$")
    agm_date: date
    doc_sha: str
    key: str | None = None  # meeting key from config.NOTICES, e.g. "tesco"


class RosterEntry(_Model):
    mention_id: str
    meeting_id: str
    resolution_no: int
    name: str
    role_text_at_issuer: str | None = None
    role_type_at_issuer: RoleType | None = None  # None: role not stated (M&S)
    coverage: Coverage
    evidence: list[Evidence] = []


class Seat(_Model):
    seat_id: str
    mention_id: str | None = None  # set when the seat comes from a notice
    officer_id: str | None = None  # set when the seat comes from CH
    org_name: str
    org_key: str
    ch_company_number: str | None = None
    role_text: str | None = None
    role_type: RoleType = RoleType.other
    board_seat: TriField = "unknown"
    chair: TriField = "unknown"
    executive: TriField = "unknown"
    current: TriField = "unknown"
    listed: TriField = "unknown"
    identity: TriField = "unknown"
    status: SeatStatus | None = None  # None until M6 reconciles
    evidence: list[Evidence] = []


class CHAppointment(_Model):
    company_number: str
    company_name: str
    company_type: str | None = None
    role: str  # CH officer_role, e.g. "director"
    appointed_on: date | None = None
    resigned_on: date | None = None


class CHOfficer(_Model):
    officer_id: str
    name: str
    surname: str | None = None
    forenames: str | None = None
    dob_month: int | None = None
    dob_year: int | None = None
    appointments: list[CHAppointment] = []


class OrgFacts(_Model):
    org_key: str
    canonical_name: str
    org_type: str | None = None  # company, charity, public body, education, partnership
    listed: TriField = "unknown"
    market: str | None = None  # e.g. "LSE Main Market", "AIM", "NYSE"
    equity_listing: TriField = "unknown" 
    parent_name: str | None = None
    events: list[str] = []  # "2024-11-07: taken private by EQT"
    evidence: list[Evidence] = []


class PositionFacts(_Model):
    seat_id: str
    board_seat: TriField = "unknown"
    chair: TriField = "unknown"
    executive: TriField = "unknown"
    current: TriField = "unknown"
    role_type: RoleType | None = None
    evidence: list[Evidence] = []


class Person(_Model):
    person_id: str
    mention_ids: list[str]
    officer_ids: list[str] = []
    evidence: list[Evidence] = []  # why the mentions and CH records were joined


class Recommendation(_Model):
    person_id: str
    meeting_id: str
    role_at_meeting: RoleType | None = None
    points_lo: int
    points_hi: int
    points_rule: TriField
    exec_rule: TriField
    vote: Vote
    coverage: Coverage
    reasons: list[str] = []
    unknown_facts: list[str] = []  # e.g. "seat:<id>.board_seat"; each one could flip the vote
    evidence: list[Evidence] = []


class ReviewItem(_Model):
    kind: str  # e.g. "unanchored", "possible_same_person", "stale", "ungrounded_quote"
    subject: str  # the id it concerns (mention, seat, org, person)
    detail: str
    suggested_command: str | None = None


class Decision(_Model):
    decision_id: str  # "dec:<n>", assigned by the database
    kind: DecisionKind
    payload: dict[str, Any]
    author: str
    reason: str
    created_at: datetime


# --- run_outputs kinds -------------------------------------------------------

OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "meeting": Meeting,
    "roster": RosterEntry,
    "seat": Seat,
    "person": Person,
    "org": OrgFacts,
    "recommendation": Recommendation,
    "review": ReviewItem,
    "officer": CHOfficer,
    "position": PositionFacts,  # a firm's own website on one seat's role (classify/positions)
}


# --- stage interface ---------------------------------------------------------


@dataclass
class RunContext:
    conn: sqlite3.Connection
    run_id: str
    settings: Settings
    policy: Policy
    offline: bool


class StageReport(_Model):
    stage: str  # extract, ch, dedup, classify, score
    ok: bool
    counts: dict[str, int] = {}
    errors: list[str] = []
