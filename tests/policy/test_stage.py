"""M6.1 run(ctx): every fixture person seeded into one run, scored through run_outputs."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pennant.core import db
from pennant.core.models import ReviewItem, RosterEntry, Vote
from pennant.policy import stage
from pennant.state.decisions import unhonoured
from tests.policy.seat_cases import CASES, load_case, meetings, seed, with_policy

MEETINGS = meetings()


@pytest.fixture
def cases():
    return [load_case(c) for c in CASES]


def recs_by_case(ctx, cases):
    recs = {(r.meeting_id, r.person_id): r
            for r in db.get_outputs(ctx.conn, ctx.run_id, "recommendation")}
    return {(c.name, k): recs[(MEETINGS[k].meeting_id, c.person.person_id)]
            for c in cases for k in c.expect}


def test_run_scores_every_roster_entry_through_run_outputs(ctx, cases):
    seed(ctx, cases)
    report = stage.run(ctx)
    assert report.ok and report.stage == "score"
    assert report.counts["recommendations"] == sum(len(c.roster) for c in cases)
    for (name, key), rec in recs_by_case(ctx, cases).items():
        exp = next(c for c in cases if c.name == name).expect[key]
        assert (rec.vote, [rec.points_lo, rec.points_hi]) == (exp["vote"], exp["points"]), name
    seats = db.get_outputs(ctx.conn, ctx.run_id, "seat")
    assert all(s.status is not None for s in seats)
    kinds = {r.kind for r in db.get_outputs(ctx.conn, ctx.run_id, "review")}
    assert "undisclosed" in kinds


def test_h16_issuer_ch_outage_never_flips_a_decided_vote(ctx, cases):
    """Last night's run scored normally; tonight CH can't serve Tesco's officer list."""
    seed(ctx, cases)
    stage.run(ctx)
    before = recs_by_case(ctx, cases)
    tonight = replace(ctx, run_id=db.create_run(ctx.conn, "cfg"))
    seed(tonight, cases)
    tesco = MEETINGS["tesco"].meeting_id
    db.put_outputs(ctx.conn, tonight.run_id, "review", [ReviewItem(
        kind="ch_unavailable", subject=tesco, detail="officer list unavailable")])
    stage.run(tonight)
    after = recs_by_case(tonight, cases)
    for k, rec in after.items():
        if rec.meeting_id == tesco:
            assert rec.vote in (before[k].vote, Vote.REVIEW), k
    assert after[("Thierry Garnier", "tesco")].vote == Vote.REVIEW


def test_roster_entry_without_a_person_is_still_scored(ctx, cases):
    seed(ctx, cases, persons=False)
    report = stage.run(ctx)
    recs = db.get_outputs(ctx.conn, ctx.run_id, "recommendation")
    assert report.ok and len(recs) == sum(len(c.roster) for c in cases)
    assert all(r.person_id.startswith("p:nm:") for r in recs)


def test_a_scoring_error_becomes_review_and_fails_the_stage(ctx, cases):
    seed(ctx, cases)
    orphan = RosterEntry(mention_id="nm:09999999:r4", meeting_id="mtg:09999999:2026-01-01",
                         resolution_no=4, name="Nobody", coverage="none")
    db.put_outputs(ctx.conn, ctx.run_id, "roster", [orphan])
    report = stage.run(ctx)
    assert not report.ok and "nm:09999999:r4" in report.errors[0]
    rec = next(r for r in db.get_outputs(ctx.conn, ctx.run_id, "recommendation")
               if r.person_id == "p:nm:09999999:r4")
    assert rec.vote == Vote.REVIEW
    assert any(r.kind == "score_error" for r in db.get_outputs(ctx.conn, ctx.run_id, "review"))


def test_set_facts_are_written_back_so_gate_g4_sees_them_honoured(ctx, cases):
    seed(ctx, cases)
    silver = next(c for c in cases if c.name == "Caroline Silver")
    moelis = silver.seat("moelis").seat_id
    for kind, target, field in [("seat", moelis, "board_seat"),
                                ("org", "asia-airfreight-terminal-co", "listed")]:
        db.insert_decision(ctx.conn, "set_fact", {"kind": kind, "target": target,
                                                  "field": field, "value": "no"}, "expert", "IR")
    stage.run(ctx)
    out = {k: db.get_outputs(ctx.conn, ctx.run_id, k) for k in ("person", "seat", "org")}
    assert unhonoured(db.active_decisions(ctx.conn), out["person"], out["seat"], out["org"]) == []
    assert recs_by_case(ctx, cases)[("Caroline Silver", "tesco")].vote == Vote.FOR


def test_rescore_is_repeatable_and_isolates_the_aim_flag(ctx, cases):
    seed(ctx, cases)
    stage.run(ctx)
    stored = {(r.meeting_id, r.person_id): (r.vote, r.points_lo, r.points_hi)
              for r in db.get_outputs(ctx.conn, ctx.run_id, "recommendation")}
    again = {(r.meeting_id, r.person_id): (r.vote, r.points_lo, r.points_hi)
             for r in stage.rescore(ctx.conn, ctx.run_id, ctx.policy)}
    assert again == stored
    stage.run(ctx)  # scoring the same run again reads the upstream seats, not its own output
    assert {(r.meeting_id, r.person_id): (r.vote, r.points_lo, r.points_hi)
            for r in db.get_outputs(ctx.conn, ctx.run_id, "recommendation")} == stored
    votes = {flag: {(r.meeting_id, r.person_id): r.vote for r in stage.rescore(
        ctx.conn, ctx.run_id, with_policy(aim_counts_as_listed=flag))}
        for flag in ("unknown", "yes")}
    changed = {k for k, v in votes["yes"].items() if v != votes["unknown"][k]}
    gilliland = next(c for c in cases if c.name == "Stewart Gilliland").person.person_id
    assert changed == {(MEETINGS["tesco"].meeting_id, gilliland)}
