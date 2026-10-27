"""M7.2: diff two runs; every change names its likely causes."""

from pennant.core import db
from pennant.core.cache import put_cache
from pennant.core.config import load_policy
from pennant.core.models import ReviewItem
from pennant.state import decisions as dec
from pennant.state.diff import diff, render, report
from pennant.state.inputs import load_outputs, recs_by_mention

from .conftest import GERRY, KENNEDY_I, KENNEDY_T, MOELIS, SILVER

KENNEDY = "p:" + KENNEDY_T  # the smaller mention id names the merged person


def recs(conn, run_id):
    return {m: r.model_dump() for m, r in recs_by_mention(load_outputs(conn, run_id)).items()}


def test_diff_against_itself_is_empty(conn, run):
    r = run(publish=True).run_id
    assert diff(conn, r, r) == []
    assert report(conn, r, r) == f"no changes from {r} to {r}"


def test_rerun_with_nothing_changed_is_empty(conn, run):
    a, b = run(publish=True).run_id, run(publish=True).run_id
    assert diff(conn, a, b) == []


def test_merge_shows_with_the_vote_changes_under_it(conn, run):
    a = run(publish=True).run_id
    d = dec.add_decision(conn, "must_link", {"a": KENNEDY_T, "b": KENNEDY_I}, "expert",
                         "same DOB 1964-03")
    b = run(publish=True, author="expert").run_id
    changes = diff(conn, a, b)
    assert [c.kind for c in changes] == ["merged"]
    merged = changes[0]
    assert merged.detail == f"{KENNEDY} merged with p:{KENNEDY_I}"
    assert merged.causes == [d.decision_id]
    assert [c.subject for c in merged.children] == [KENNEDY_T, KENNEDY_I]
    assert all("vote FOR → AGAINST" in c.detail and c.causes == ["dec:1"]
               for c in merged.children)
    text = render(changes)
    assert text.splitlines()[0] == f"{KENNEDY} merged with p:{KENNEDY_I} (dec:1)"
    assert text.splitlines()[1].startswith("  Chris Kennedy @ tesco: vote FOR → AGAINST")
    full = report(conn, a, b)
    assert "dec:1: must_link" in full and "by expert: same DOB 1964-03" in full


def test_split_then_undo_restores_identical_recommendations(conn, run, world):
    world.links = [(KENNEDY_T, KENNEDY_I)]  # the DOB matcher joined them
    a = run(publish=True).run_id
    split = dec.add_decision(conn, "cannot_link", {"a": [KENNEDY_I], "b": [KENNEDY_T]}, "expert",
                             "different people")
    b = run(publish=True, author="expert").run_id
    (change,) = diff(conn, a, b)
    assert change.kind == "split" and change.causes == [split.decision_id]
    assert change.detail == f"{KENNEDY} split into {KENNEDY}, p:{KENNEDY_I}"
    assert all("AGAINST → FOR" in c.detail for c in change.children)

    undo = dec.revoke(conn, split.decision_id, "expert", "same person after all")
    c = run(publish=True, author="expert").run_id
    (back,) = diff(conn, b, c)
    assert back.kind == "merged" and back.causes == [undo.decision_id]
    assert diff(conn, a, c) == []
    assert recs(conn, a) == recs(conn, c)


def test_set_fact_tags_the_seat_and_the_vote(conn, run):
    a = run(publish=True).run_id
    d = dec.add_decision(conn, "set_fact", {"kind": "seat", "target": MOELIS,
                                            "field": "board_seat", "value": "no"},
                         "expert", "Moelis role is advisory")
    b = run(publish=True, author="expert").run_id
    seat, vote = diff(conn, a, b)
    assert seat.kind == "seat" and seat.subject == MOELIS
    assert seat.detail == f"seat {MOELIS} (Moelis & Company): board_seat unknown → no"
    assert vote.subject == SILVER and vote.causes == seat.causes == [d.decision_id]
    assert vote.detail == ("Caroline Silver @ tesco: vote REVIEW → FOR; points 4–5 → 4–4; "
                           "points_rule unknown → no")


def test_policy_change_is_named(conn, run, world):
    a = run(publish=True, policy=load_policy().model_copy(
        update={"aim_counts_as_listed": "unknown"})).run_id
    world.votes[GERRY] = "REVIEW"
    policy = load_policy().model_copy(update={"aim_counts_as_listed": "yes"})
    b = run(policy=policy).run_id
    (change,) = diff(conn, a, b)
    assert change.causes == ["policy aim_counts_as_listed: unknown → yes"]


def test_cache_change_is_named(conn, run, world):
    a = run(publish=True).run_id
    put_cache(conn, "web", "k1", {"listed": "no"})
    world.votes[GERRY] = "REVIEW"
    b = run().run_id
    (change,) = diff(conn, a, b)
    assert change.causes == ["cache: 1 web changed"]


def test_unrelated_decision_is_offered_as_a_possible_cause(conn, run, world):
    a = run(publish=True).run_id
    d = dec.add_decision(conn, "set_fact", {"kind": "org", "target": "moelis",
                                            "field": "listed", "value": "yes"}, "expert", "NYSE")
    world.votes[GERRY] = "REVIEW"
    b = run(author="expert").run_id
    gerry = next(c for c in diff(conn, a, b) if c.subject == GERRY)
    assert gerry.causes == [d.decision_id]


def test_unexplained_change_says_so(conn, run, world):
    a = run(publish=True).run_id
    world.votes[GERRY] = "REVIEW"
    b = run().run_id
    (change,) = diff(conn, a, b)
    assert change.causes == ["no recorded input change"]


def test_first_run_reports_everything_new(conn, run):
    b = run(publish=True).run_id
    changes = diff(conn, None, b)
    votes = [c for c in changes if c.kind == "recommendation"]
    assert len(votes) == 5
    assert any(c.detail == "Caroline Silver @ tesco: new: REVIEW at 4–5 points" for c in votes)
    assert db.published_run_id(conn) == b


def test_outage_is_named_as_a_cause(conn, run, world):
    """A CH outage changes answers with no input change; diff names the outage."""
    a = run(publish=True).run_id
    real = world.ch

    def ch(ctx):
        rep = real(ctx)
        db.put_outputs(ctx.conn, ctx.run_id, "review", [ReviewItem(
            kind="ch_unavailable", subject=f"off-{SILVER}", detail="appointments unavailable")])
        return rep

    world.ch = ch
    world.votes[SILVER] = "FOR"
    b = run().run_id  # held by G2, but still diffable
    (change,) = diff(conn, a, b)
    assert change.causes == [f"ch_unavailable: off-{SILVER}"]
    assert "no recorded input change" not in report(conn, a, b)
