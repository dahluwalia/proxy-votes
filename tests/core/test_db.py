"""M0.2: tables, run outputs, runs and the publish pointer, decisions."""

import sqlite3

import pytest

from pennant.core import db
from pennant.core.cache import put_cache
from pennant.core.models import Person, ReviewItem, Seat


def test_init_twice_is_safe(tmp_path):
    path = tmp_path / "p.db"
    conn = db.connect(path)
    db.init_db(conn)
    run_id = db.create_run(conn, "h")
    conn.close()
    conn = db.connect(path)  # initialises again on an existing file
    db.init_db(conn)
    assert db.get_run(conn, run_id)["status"] == "running"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"cache", "decisions", "runs", "run_outputs"} <= tables


def test_put_and_get_outputs_latest_wins(ctx):
    s1 = Seat(seat_id="b", org_name="X", org_key="x")
    s2 = Seat(seat_id="a", org_name="Y", org_key="y")
    assert db.put_outputs(ctx.conn, ctx.run_id, "seat", [s1, s2]) == 2
    refined = s1.model_copy(update={"board_seat": "yes"})
    db.put_outputs(ctx.conn, ctx.run_id, "seat", [refined])
    seats = db.get_outputs(ctx.conn, ctx.run_id, "seat")
    assert [s.seat_id for s in seats] == ["a", "b"]
    assert seats[1].board_seat == "yes"
    assert db.get_outputs(ctx.conn, ctx.run_id, "person") == []
    assert db.get_outputs(ctx.conn, "run:999", "seat") == []


def test_outputs_are_per_run(ctx):
    other = db.create_run(ctx.conn, "h")
    db.put_outputs(ctx.conn, ctx.run_id, "person", [Person(person_id="p:1", mention_ids=["1"])])
    assert db.get_outputs(ctx.conn, other, "person") == []


def test_put_outputs_checks_kind_and_type(ctx):
    with pytest.raises(ValueError):
        db.put_outputs(ctx.conn, ctx.run_id, "nonsense", [])
    with pytest.raises(TypeError):
        db.put_outputs(ctx.conn, ctx.run_id, "person", [Seat(seat_id="s", org_name="X",
                                                             org_key="x")])


def test_review_items_keyed_by_content(ctx):
    items = [ReviewItem(kind="stale", subject="s1", detail="a"),
             ReviewItem(kind="stale", subject="s1", detail="b")]
    db.put_outputs(ctx.conn, ctx.run_id, "review", items)
    assert len(db.get_outputs(ctx.conn, ctx.run_id, "review")) == 2


def test_run_lifecycle_and_publish_pointer(ctx):
    conn = ctx.conn
    assert ctx.run_id == "run:1"
    assert db.published_run_id(conn) is None
    db.finish_run(conn, ctx.run_id, "staged", {"G1": "pass"})
    run = db.get_run(conn, ctx.run_id)
    assert run["status"] == "staged" and run["gate_report"] == {"G1": "pass"}
    db.publish(conn, ctx.run_id)
    r2 = db.create_run(conn, "h")
    db.finish_run(conn, r2, "held", {"G2": "fail"})
    assert db.published_run_id(conn) == "run:1"
    with pytest.raises(sqlite3.IntegrityError, match="staged"):
        db.publish(conn, r2)  # a held run never becomes the published one
    r3 = db.create_run(conn, "h")
    db.finish_run(conn, r3, "staged", {})
    db.publish(conn, r3)
    assert db.published_run_id(conn) == r3
    db.publish(conn, ctx.run_id)  # rollback to an earlier published run
    assert db.published_run_id(conn) == "run:1"
    assert [r["run_id"] for r in db.list_runs(conn)] == ["run:1", "run:2", "run:3"]
    with pytest.raises(ValueError):
        db.finish_run(conn, r2, "published", None)


def test_revoked_decision_is_hidden(ctx):
    conn = ctx.conn
    d1 = db.insert_decision(conn, "must_link", {"a": "m1", "b": "m2"}, "expert", "same DOB")
    d2 = db.insert_decision(conn, "set_fact", {"subject": "s1", "field": "board_seat",
                                               "value": "no"}, "expert", "checked")
    assert (d1.decision_id, d2.decision_id) == ("dec:1", "dec:2")
    undo = db.insert_decision(conn, "revoke", {"decision_id": "dec:1"}, "expert", "oops")
    assert [d.decision_id for d in db.active_decisions(conn)] == ["dec:2"]
    db.insert_decision(conn, "revoke", {"decision_id": undo.decision_id}, "expert", "undo undo")
    assert [d.decision_id for d in db.active_decisions(conn)] == ["dec:1", "dec:2"]
    assert len(db.all_decisions(conn)) == 4
    assert db.active_decisions(conn)[0].payload == {"a": "m1", "b": "m2"}


@pytest.mark.parametrize("sql", [
    "UPDATE decisions SET reason = 'edited'",
    "DELETE FROM decisions",
    "UPDATE run_outputs SET payload = '{}'",
    "DELETE FROM run_outputs",
    "UPDATE cache SET payload = '{}'",
    "DELETE FROM cache",
    "DELETE FROM runs",
    "UPDATE runs SET author = 'someone else'",
    "UPDATE runs SET status = 'held'",  # a finished run's status never changes
])
def test_history_cannot_be_edited_or_deleted(ctx, sql):
    """Append-only is enforced by the database, so no code path (or sqlite3 session) can
    rewrite a decision, a cached answer or a past run's results."""
    conn = ctx.conn
    db.insert_decision(conn, "set_fact", {"kind": "org", "target": "x", "field": "listed",
                                          "value": "yes"}, "expert", "why")
    db.put_outputs(conn, ctx.run_id, "review", [ReviewItem(kind="k", subject="s", detail="d")])
    put_cache(conn, "ch", "url", {"status": 200, "body": {}})
    db.finish_run(conn, ctx.run_id, "staged", {})
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(sql)
