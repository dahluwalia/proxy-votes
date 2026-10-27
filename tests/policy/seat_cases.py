"""Load the hand-written seat fixtures (tests/fixtures/seats/) into models and score them.

Design choice: each YAML file is one person as the upstream stages would hand them to
scoring; this loader fills the ids (mention, seat, org key) so the files stay readable.
Import it as `tests.policy.seat_cases` (conftest helpers aren't importable under
pytest's importlib mode).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from pennant.classify.positions import merge_position
from pennant.classify.research import org_key as slug
from pennant.core import db
from pennant.core.config import FIXTURES_DIR, Policy, load_policy
from pennant.core.models import (
    CHOfficer,
    Decision,
    Meeting,
    OrgFacts,
    Person,
    PositionFacts,
    Recommendation,
    ReviewItem,
    RosterEntry,
    Seat,
    mention_id,
    person_id,
)
from pennant.policy.reconcile import reconcile
from pennant.policy.score import score
from pennant.policy.stage import issuer_orgs

SEATS_DIR = FIXTURES_DIR / "seats"
CASES = sorted(p.stem for p in SEATS_DIR.glob("*.yaml") if p.stem != "meetings")


def meetings() -> dict[str, Meeting]:
    raw = yaml.safe_load((SEATS_DIR / "meetings.yaml").read_text())
    return {k: Meeting(**v) for k, v in raw.items()}


@dataclass
class Case:
    name: str
    person: Person
    roster: list[RosterEntry]
    seats: list[Seat]
    ch: list[CHOfficer] | None
    orgs: list[OrgFacts]
    expect: dict[str, dict[str, Any]]
    meetings: dict[str, Meeting] = field(default_factory=meetings)
    positions: list[PositionFacts] = field(default_factory=list)  # roles from firms' own sites

    def seat(self, fragment: str) -> Seat:
        """The one seat whose id contains `fragment`."""
        found = [s for s in self.seats if fragment in s.seat_id]
        assert len(found) == 1, (fragment, [s.seat_id for s in found])
        return found[0]


def load_case(stem: str) -> Case:
    raw = yaml.safe_load((SEATS_DIR / f"{stem}.yaml").read_text())
    mtgs = meetings()
    roster, mentions = [], {}
    for r in raw["roster"]:
        r = dict(r)
        m = mtgs[r.pop("meeting")]
        mentions[m.key] = mention_id(m.company_number, r["resolution_no"])
        roster.append(RosterEntry(mention_id=mentions[m.key], meeting_id=m.meeting_id, **r))
    seats: list[Seat] = []
    for s in raw.get("seats") or []:
        s = dict(s)
        mid = mentions[s.pop("meeting")]
        s.setdefault("org_key", slug(s["org_name"]))
        sid = base = f"seat:{mid}:{s['org_key']}"
        n = 1
        while any(x.seat_id == sid for x in seats):
            n += 1
            sid = f"{base}-{n}"
        seats.append(Seat(seat_id=s.pop("seat_id", sid), mention_id=mid, **s))
    ch = None if raw.get("ch") is None else [CHOfficer(**o) for o in raw["ch"]]
    ids = sorted(mentions.values())
    person = Person(person_id=person_id(ids), mention_ids=ids,
                    officer_ids=[o.officer_id for o in ch or []])
    orgs = [OrgFacts(**{"org_key": slug(o["canonical_name"]), **o}) for o in raw.get("orgs") or []]
    positions = [PositionFacts(**p) for p in raw.get("positions") or []]
    return Case(name=raw["name"], person=person, roster=roster, seats=seats, ch=ch, orgs=orgs,
                expect=raw.get("expect") or {}, meetings=mtgs, positions=positions)


@dataclass
class Scored:
    rec: Recommendation
    seats: list[Seat]
    items: list[ReviewItem]

    def seat(self, fragment: str) -> Seat:
        found = [s for s in self.seats if fragment in s.seat_id]
        assert len(found) == 1, (fragment, [s.seat_id for s in found])
        return found[0]


def score_case(
    case: Case, policy: Policy | None = None, decisions: tuple[Decision, ...] = (),
    ch: Any = "case",
) -> dict[str, Scored]:
    """Reconcile and score every roster entry of the case, keyed by meeting key.
    Pass `ch=None` to simulate Companies House being unavailable."""
    policy = policy or load_policy()
    by_id = {m.meeting_id: m for m in case.meetings.values()}
    out = {}
    for e in case.roster:
        m = by_id[e.meeting_id]
        orgs = [*case.orgs, *issuer_orgs(case.meetings.values())]
        records = case.ch if ch == "case" else ch
        seats, items = reconcile(case.person, m, case.seats, records, orgs)
        found = {p.seat_id: p for p in case.positions}  # as the score stage applies them
        seats = [merge_position(s, found[s.seat_id]) if s.seat_id in found else s for s in seats]
        rec = score(case.person, e, m, seats, orgs, policy, list(decisions),
                    ch_complete=records is not None)
        out[m.key] = Scored(rec=rec, seats=seats, items=items)
    return out


def with_policy(**flags: Any) -> Policy:
    return load_policy().model_copy(update=flags)


def seed(ctx: Any, cases: list[Case], persons: bool = True) -> None:
    """Write the cases into ctx's run as the upstream stages would (all five meetings)."""
    def put(kind: str, models: Any) -> None:
        db.put_outputs(ctx.conn, ctx.run_id, kind, list(models))

    put("meeting", meetings().values())
    put("roster", [e for c in cases for e in c.roster])
    put("seat", [s for c in cases for s in c.seats])
    put("person", [c.person for c in cases] if persons else [])
    put("officer", [o for c in cases for o in c.ch or []])
    orgs: dict[str, OrgFacts] = {}
    for c in cases:
        for o in c.orgs:
            orgs.setdefault(o.org_key, o)
    put("org", orgs.values())
    put("position", [p for c in cases for p in c.positions])
