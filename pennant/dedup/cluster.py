"""M4.3: cluster notice mentions and CH officers into people.

Design choice: a plain union-find over notice mention ids and CH officer ids, no
scores. Analyst must-links are applied first, then matcher links in a fixed order
(anchor links before date-of-birth links, then by id), so shuffled input gives the
same people. Any union that would put a cannot-link pair in one person is refused
and becomes a review item; cannot-links always win, because a wrong merge adds
seats and can cause a wrong AGAINST. Two mentions in one notice are always a
cannot-link pair: a notice never proposes one person twice. A person is `p:` plus its
smallest mention id, and carries the evidence of every link that joined it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import combinations, product

from pennant.core import db
from pennant.core.models import (
    CHOfficer,
    Decision,
    Evidence,
    Person,
    ReviewItem,
    RosterEntry,
    RunContext,
    StageReport,
    person_id,
)
from pennant.dedup import anchor as matcher  # tests swap this module

LINK_KINDS = ("anchor", "dob")  # application order: issuer anchor, then DOB expansion
SAME_NOTICE = "same-notice:"  # blocker id prefix for the implicit same-notice cannot-link


@dataclass(frozen=True)
class Link:
    """Two nodes (mention ids or CH officer ids) the matcher says are one person."""

    a: str
    b: str
    kind: str = "anchor"
    evidence: tuple[Evidence, ...] = field(default=(), compare=False)

    def canonical(self) -> Link:
        return self if self.a <= self.b else Link(self.b, self.a, self.kind, self.evidence)

    def sort_key(self) -> tuple[int, str, str]:
        rank = LINK_KINDS.index(self.kind) if self.kind in LINK_KINDS else len(LINK_KINDS)
        return rank, min(self.a, self.b), max(self.a, self.b)


def is_mention(node: str) -> bool:
    return node.startswith("nm:")


def node_of(ref: str) -> str:
    """A decision id reference: a person id `p:nm:...` stands for that mention."""
    return ref.removeprefix("p:")


def _ids(value: str | list[str]) -> list[str]:
    return [node_of(v) for v in ([value] if isinstance(value, str) else value)]


def _dec_no(d: Decision) -> int:
    m = re.search(r"\d+$", d.decision_id)
    return int(m.group()) if m else 0


class _UnionFind:
    def __init__(self, nodes: Iterable[str]) -> None:
        self.parent = {n: n for n in nodes}
        self.members = {n: {n} for n in self.parent}
        self.apart: dict[str, dict[str, str]] = {}  # node -> {other node: decision id}

    def add(self, *nodes: str) -> None:
        for n in nodes:
            if n not in self.parent:
                self.parent[n], self.members[n] = n, {n}

    def find(self, n: str) -> str:
        while self.parent[n] != n:
            self.parent[n] = self.parent[self.parent[n]]
            n = self.parent[n]
        return n

    def keep_apart(self, a: str, b: str, decision_id: str) -> None:
        self.apart.setdefault(a, {})[b] = decision_id
        self.apart.setdefault(b, {})[a] = decision_id

    def blocker(self, a: str, b: str) -> str | None:
        """The cannot-link decision that forbids joining a's and b's groups, if any."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return None
        other = self.members[rb]
        for x in sorted(self.members[ra]):
            hits = sorted(y for y in self.apart.get(x, {}) if y in other)
            if hits:
                return self.apart[x][hits[0]]
        return None

    def union(self, a: str, b: str) -> None:
        ra, rb = sorted((self.find(a), self.find(b)))
        if ra != rb:
            self.parent[rb] = ra
            self.members[ra] |= self.members.pop(rb)


def _apart(blocker: str) -> tuple[str, str | None]:
    """(why a blocker keeps two groups apart, the command that would lift it)."""
    if blocker.startswith(SAME_NOTICE):
        return f"both are named in {blocker.removeprefix(SAME_NOTICE)}, so are two people", None
    return f"kept apart by {blocker}", f"pennant undo {blocker}"


def _pid(uf: _UnionFind, node: str) -> str:
    mentions = [n for n in uf.members[uf.find(node)] if is_mention(n)]
    return person_id(mentions) if mentions else node


def _evidence_key(link: Link) -> tuple:
    return link.sort_key(), [e.model_dump_json() for e in link.evidence]


def cluster(
    mentions: Iterable[str], links: Iterable[Link], decisions: Iterable[Decision],
    meeting_of: Mapping[str, str] | None = None,
) -> tuple[list[Person], list[ReviewItem]]:
    """Group mentions into people. Returns people sorted by id, plus review items for
    refused links and decisions that could not be honoured. `meeting_of` maps a mention
    to its meeting id; without it, a mention id's company number stands for its notice."""
    mentions = sorted(set(mentions))
    links = sorted((link.canonical() for link in links), key=_evidence_key)
    decisions = sorted(decisions, key=_dec_no)
    uf = _UnionFind(mentions)
    reviews: list[ReviewItem] = []
    known = set(mentions)

    notices: dict[str, list[str]] = defaultdict(list)
    for m in mentions:
        notices[(meeting_of or {}).get(m) or "notice " + m.rsplit(":", 1)[0]].append(m)
    for notice, ms in notices.items():
        for x, y in combinations(ms, 2):
            uf.keep_apart(x, y, SAME_NOTICE + notice)

    def stale(d: Decision, ids: list[str]) -> bool:
        missing = [n for n in ids if is_mention(n) and n not in known]
        if missing:
            reviews.append(ReviewItem(
                kind="decision_stale", subject=d.decision_id,
                detail=f"{d.kind} names mentions not in this run: {', '.join(missing)}",
                suggested_command=f"pennant undo {d.decision_id}",
            ))
        return bool(missing)

    for d in decisions:
        if d.kind == "cannot_link":
            left, right = _ids(d.payload["a"]), _ids(d.payload["b"])
            if not stale(d, left + right):
                for x, y in product(left, right):
                    uf.add(x, y)
                    uf.keep_apart(x, y, d.decision_id)

    refused: list[tuple[Link, str]] = []
    for d in decisions:
        if d.kind != "must_link":
            continue
        a, b = node_of(d.payload["a"]), node_of(d.payload["b"])
        if stale(d, [a, b]):
            continue
        uf.add(a, b)
        if blocker := uf.blocker(a, b):
            why, undo = _apart(blocker)
            reviews.append(ReviewItem(
                kind="decision_conflict", subject=d.decision_id,
                detail=f"must-link {a} – {b} refused: {why}",
                suggested_command=undo or f"pennant undo {d.decision_id}",
            ))
        else:
            uf.union(a, b)

    applied: list[Link] = []
    for link in links:
        uf.add(link.a, link.b)
        if blocker := uf.blocker(link.a, link.b):
            refused.append((link, blocker))
        else:
            uf.union(link.a, link.b)
            applied.append(link)

    for link, blocker in refused:
        pa, pb = sorted((_pid(uf, link.a), _pid(uf, link.b)))
        why, undo = _apart(blocker)
        reviews.append(ReviewItem(
            kind="link_refused", subject=pa,
            detail=f"{link.kind} link {link.a} – {link.b} refused: it would join {pa} and "
            f"{pb}; {why}",
            suggested_command=undo,
        ))

    evidence: dict[str, dict[str, Evidence]] = defaultdict(dict)  # root -> {json: ev}
    for link in applied:  # in link order, so a person's evidence order is fixed
        for e in link.evidence:
            evidence[uf.find(link.a)].setdefault(e.model_dump_json(), e)

    people = []
    for root in {uf.find(n) for n in mentions}:
        group = uf.members[root]
        ms = sorted(n for n in group if is_mention(n))
        officers = sorted(n for n in group if not is_mention(n))
        people.append(Person(person_id=person_id(ms), mention_ids=ms, officer_ids=officers,
                             evidence=list(evidence[root].values())))
    return sorted(people, key=lambda p: p.person_id), reviews


# --- stage -------------------------------------------------------------------


def _issuer_officers(officers: list[CHOfficer], company_number: str) -> list[CHOfficer]:
    return [o for o in officers if any(a.company_number == company_number for a in o.appointments)]


def _match(
    entry: RosterEntry, issuer: list[CHOfficer], ctx: RunContext
) -> tuple[list[Link], list[CHOfficer], list[ReviewItem]]:
    """Anchor the entry to one issuer officer, then expand that officer to its other CH
    records by date of birth (both in dedup/anchor.py)."""
    officer, reviews = matcher.anchor(entry, issuer)
    if officer is None:
        return [], [], list(reviews)
    evidence = tuple(matcher.anchor_evidence(entry, officer))
    links = [Link(entry.mention_id, officer.officer_id, "anchor", evidence)]
    found = []
    if any(r.kind == "not_in_office" for r in reviews):
        # a former director is stored with just the issuer appointment: load the record
        record, items = matcher.fetch_officer(officer.officer_id, ctx)
        found += [record] if record else []
        reviews = [*reviews, *items]
    expanded, more = matcher.expand(officer, ctx)
    for other, evidence in expanded:
        links.append(Link(officer.officer_id, other.officer_id, "dob", tuple(evidence)))
        found.append(other)
    return links, found, list(reviews) + list(more)


def run(ctx: RunContext) -> StageReport:
    """Anchor and expand every roster entry, cluster under the active decisions, and
    write `person` and `review` outputs (plus any newly found `officer` records).

    Reads the issuer officer lists the CH stage wrote as `officer` outputs. Where an
    issuer's list is unavailable (the CH stage wrote a `ch_unavailable` review item and
    no officers), its entries are not anchored and stay one person per mention; gate G2
    already holds such a run. An entry whose meeting is missing, or whose issuer has no
    officers on record with no such CH item, also stays alone and gets `unanchored`."""
    roster = db.get_outputs(ctx.conn, ctx.run_id, "roster")
    meetings = {m.meeting_id: m for m in db.get_outputs(ctx.conn, ctx.run_id, "meeting")}
    officers = db.get_outputs(ctx.conn, ctx.run_id, "officer")
    unavailable = {
        r.subject for r in db.get_outputs(ctx.conn, ctx.run_id, "review")
        if r.kind == "ch_unavailable"
    }
    known = {o.officer_id: len(o.appointments) for o in officers}  # id -> appointments held
    links: list[Link] = []
    reviews: list[ReviewItem] = []
    new_officers: dict[str, CHOfficer] = {}
    errors: list[str] = []
    skipped = 0

    for entry in roster:
        meeting = meetings.get(entry.meeting_id)
        issuer = _issuer_officers(officers, meeting.company_number) if meeting else []
        if entry.meeting_id in unavailable:
            skipped += 1  # CH stage already raised ch_unavailable for this meeting
            continue
        if not issuer:
            why = f"CH has no officers on record for {meeting.company_number}" if meeting \
                else f"meeting {entry.meeting_id} is not in this run"
            reviews.append(ReviewItem(
                kind="unanchored", subject=entry.mention_id,
                detail=f"{entry.name}: not matched to CH, because {why}",
                suggested_command="pennant run"))
            continue
        try:
            found_links, found, items = _match(entry, issuer, ctx)
        except Exception as exc:  # fail closed: this mention stays alone, flagged
            errors.append(f"{entry.mention_id}: {type(exc).__name__}: {exc}")
            reviews.append(ReviewItem(
                kind="unanchored", subject=entry.mention_id,
                detail=f"{entry.name}: matching failed ({type(exc).__name__}: {exc})",
            ))
            continue
        links += found_links
        reviews += items
        new_officers.update({o.officer_id: o for o in found
                             if len(o.appointments) > known.get(o.officer_id, -1)})

    decisions = db.active_decisions(ctx.conn)
    people, cluster_reviews = cluster([e.mention_id for e in roster], links, decisions,
                                      {e.mention_id: e.meeting_id for e in roster})
    reviews += cluster_reviews
    # An officer only an analyst's merge joined may be unloaded, or a recently resigned
    # issuer director stored with just that one appointment: load the full record.
    linked = {n for link in links for n in (link.a, link.b)}
    for oid in sorted({o for p in people for o in p.officer_ids} - linked):
        record, items = matcher.fetch_officer(oid, ctx)
        new_officers.update({oid: record} if record else {})
        reviews += items
    db.put_outputs(ctx.conn, ctx.run_id, "officer", new_officers.values())
    db.put_outputs(ctx.conn, ctx.run_id, "person", people)
    db.put_outputs(ctx.conn, ctx.run_id, "review", reviews)
    return StageReport(
        stage="dedup",
        ok=not errors,
        counts={"mentions": len(roster), "people": len(people), "links": len(links),
                "reviews": len(reviews), "skipped_ch_unavailable": skipped},
        errors=errors,
    )
