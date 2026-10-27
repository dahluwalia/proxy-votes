"""Stub stages for M7: a tiny two-meeting world with knobs for each failure mode.

Tesco has Gerry Murphy (r4), Caroline Silver (r5) and Chris Kennedy (r6); ITV has
Chris Kennedy (r4) and Sharmila Nebhrajani (r5). Dedup links nothing by itself, so
the Kennedys are two people until an analyst merges them or `World.links` links
them (as the DOB matcher would); cannot-link decisions veto links. Score votes:
- Kennedy: AGAINST at both meetings once merged (exec rule), FOR otherwise;
- Silver: REVIEW, or FOR once her Moelis seat has board_seat "no";
- anyone in `World.votes`: that vote; everyone else FOR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from pennant.core import db
from pennant.core.config import Settings, load_policy
from pennant.core.models import (
    CHAppointment,
    CHOfficer,
    Evidence,
    Meeting,
    Person,
    Recommendation,
    ReviewItem,
    RosterEntry,
    RunContext,
    Seat,
    StageReport,
    meeting_id,
    mention_id,
    notice_ref,
)
from pennant.state import decisions as dec
from pennant.state import pipeline

TESCO, ITV = "00445790", "04967001"
MEETINGS = {
    TESCO: ("Tesco PLC", "2026-06-18", "tesco", "Tesco_2026_Notice_of_AGM.pdf"),
    ITV: ("ITV plc", "2026-05-07", "itv", "ITV_2026_Notice_of_AGM.pdf"),
}
ROSTER = {
    TESCO: {4: "Gerry Murphy", 5: "Caroline Silver", 6: "Chris Kennedy"},
    ITV: {4: "Chris Kennedy", 5: "Sharmila Nebhrajani"},
}
GERRY, SILVER, KENNEDY_T = (mention_id(TESCO, n) for n in (4, 5, 6))
KENNEDY_I, NEBHRAJANI = (mention_id(ITV, n) for n in (4, 5))
MOELIS = f"seat:{SILVER}:moelis"
GROUNDED = "Chair: Barratt Redrow plc and ICE Clear Europe"


@dataclass
class World:
    votes: dict[str, str] = field(default_factory=dict)  # mention -> forced vote
    gaps: dict[str, tuple[int, ...]] = field(default_factory=dict)  # company -> resolution nos
    ch_down: set[str] = field(default_factory=set)  # issuers whose officer list fails
    fabricate: str | None = None  # mention whose issuer seat cites a fabricated quote
    boom: str | None = None  # stage that raises
    skip_rec: set[str] = field(default_factory=set)  # mentions left without a vote
    ch_silent: bool = False  # CH drops the ch_down issuers without saying so
    ignore_decisions: bool = False  # score forgets to apply set_fact
    ignore_links: bool = False  # dedup forgets must_link decisions
    links: list[tuple[str, str]] = field(default_factory=list)  # matcher links (e.g. DOB)

    def stages(self) -> list[tuple[str, Any]]:
        return [(n, self._wrap(n, f)) for n, f in (
            ("extract", self.extract), ("ch", self.ch), ("dedup", self.dedup),
            ("classify", lambda ctx: StageReport(stage="classify", ok=True)),
            ("score", self.score))]

    def _wrap(self, name, fn):
        def stage(ctx: RunContext) -> StageReport:
            if self.boom == name:
                raise RuntimeError(f"{name} blew up")
            return fn(ctx)
        return stage

    def extract(self, ctx: RunContext) -> StageReport:
        meetings, roster, seats = [], [], []
        for co, (name, agm, key, doc) in MEETINGS.items():
            mid = meeting_id(co, agm)
            meetings.append(Meeting(meeting_id=mid, company_name=name, company_number=co,
                                    agm_date=agm, doc_sha="sha", key=key))
            for no, person in ROSTER[co].items():
                if co in self.gaps and no not in self.gaps[co]:
                    continue
                m = mention_id(co, no)
                quote = "FABRICATED NED of Standard Chartered" if m == self.fabricate else GROUNDED
                ev = Evidence(source="notice", ref=notice_ref(doc, 10), quote=quote)
                roster.append(RosterEntry(mention_id=m, meeting_id=mid, resolution_no=no,
                                          name=person, coverage="structured"))
                seats.append(Seat(seat_id=f"seat:{m}:issuer", mention_id=m, org_name=name,
                                  org_key=co, evidence=[ev]))
        seats.append(Seat(seat_id=MOELIS, mention_id=SILVER, org_name="Moelis & Company",
                          org_key="moelis", role_type="director_unspecified"))
        db.put_outputs(ctx.conn, ctx.run_id, "meeting", meetings)
        db.put_outputs(ctx.conn, ctx.run_id, "roster", roster)
        db.put_outputs(ctx.conn, ctx.run_id, "seat", seats)
        return StageReport(stage="extract", ok=True, counts={"roster": len(roster)})

    def ch(self, ctx: RunContext) -> StageReport:
        officers, review = [], []
        for r in db.get_outputs(ctx.conn, ctx.run_id, "roster"):
            co = r.mention_id.split(":")[1]
            if co in self.ch_down:
                continue
            officers.append(CHOfficer(officer_id=f"off-{r.mention_id}", name=r.name,
                                      appointments=[CHAppointment(company_number=co,
                                                                  company_name=co,
                                                                  role="director")]))
        for co in sorted(self.ch_down) if not self.ch_silent else []:
            review.append(ReviewItem(kind=pipeline.CH_UNAVAILABLE,
                                     subject=meeting_id(co, MEETINGS[co][1]),
                                     detail="officer list unavailable"))
        db.put_outputs(ctx.conn, ctx.run_id, "officer", officers)
        db.put_outputs(ctx.conn, ctx.run_id, "review", review)
        down = sorted(self.ch_down) if not self.ch_silent else []
        return StageReport(stage="ch", ok=not down, errors=[f"{co}: unavailable" for co in down])

    def dedup(self, ctx: RunContext) -> StageReport:
        mentions = [r.mention_id for r in db.get_outputs(ctx.conn, ctx.run_id, "roster")]
        parent = {m: m for m in mentions}

        def find(x):
            while parent[x] != x:
                x = parent[x]
            return x

        active = [] if self.ignore_links else db.active_decisions(ctx.conn)
        apart = {frozenset(pr) for d in active if d.kind == "cannot_link"
                 for pr in dec.link_pairs(d)}
        musts = [pr for d in active if d.kind == "must_link" for pr in dec.link_pairs(d)]
        for x, y in [*musts, *self.links]:
            if x in parent and y in parent and frozenset((x, y)) not in apart:
                parent[find(x)] = find(y)
        groups: dict[str, list[str]] = {}
        for m in mentions:
            groups.setdefault(find(m), []).append(m)
        persons = [Person(person_id=f"p:{min(ms)}", mention_ids=sorted(ms),
                          officer_ids=sorted(f"off-{m}" for m in ms)) for ms in groups.values()]
        db.put_outputs(ctx.conn, ctx.run_id, "person", persons)
        return StageReport(stage="dedup", ok=True, counts={"persons": len(persons)})

    def score(self, ctx: RunContext) -> StageReport:
        conn, run = ctx.conn, ctx.run_id
        seats = db.get_outputs(conn, run, "seat")
        if not self.ignore_decisions:
            seats = dec.apply_set_facts(seats, "seat", db.active_decisions(conn))
            db.put_outputs(conn, run, "seat", seats)
        moelis = next(s for s in seats if s.seat_id == MOELIS)
        meeting_of = {r.mention_id: r.meeting_id for r in db.get_outputs(conn, run, "roster")}
        recs = []
        for p in db.get_outputs(conn, run, "person"):
            for m in p.mention_ids:
                if m in self.skip_rec or m not in meeting_of:
                    continue
                if m in self.votes:
                    vote, lo, hi = self.votes[m], 1, 1
                elif m in (KENNEDY_T, KENNEDY_I):
                    vote, lo, hi = ("AGAINST", 2, 2) if len(p.mention_ids) > 1 else ("FOR", 1, 1)
                elif m == SILVER:
                    vote, lo, hi = ("FOR", 4, 4) if moelis.board_seat == "no" else ("REVIEW", 4, 5)
                else:
                    vote, lo, hi = "FOR", 1, 1
                recs.append(Recommendation(
                    person_id=p.person_id, meeting_id=meeting_of[m], points_lo=lo, points_hi=hi,
                    points_rule="no" if vote == "FOR" else "unknown",
                    exec_rule="yes" if vote == "AGAINST" else "no",
                    vote=vote, coverage="structured"))
        db.put_outputs(conn, run, "recommendation", recs)
        return StageReport(stage="score", ok=True, counts={"recommendations": len(recs)})


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def run(conn, world):
    """run(publish=False, author="system", policy=None) -> RunResult, on the stub world."""
    settings = Settings(db_path=":memory:", offline=True)

    def _run(publish: bool = False, author: str = "system", policy=None):
        return pipeline.run_pipeline(settings, offline=True, publish=publish, author=author,
                                     conn=conn, policy=policy or load_policy(),
                                     stages=world.stages())
    return _run


REAL_SEEDS = pipeline.OFFLINE_SEEDS  # the committed CH and web fixture loaders


@pytest.fixture(autouse=True)
def no_fixture_seeding(monkeypatch):
    """The stub world needs no recorded CH or web answers; seeding has its own tests."""
    monkeypatch.setattr(pipeline, "OFFLINE_SEEDS", ())


@pytest.fixture(autouse=True)
def fake_grounding(monkeypatch):
    """Lane B tests real grounding; here a quote grounds unless it says FABRICATED."""
    monkeypatch.setattr(pipeline, "quote_grounded", lambda ev: "FABRICATED" not in ev.quote)
