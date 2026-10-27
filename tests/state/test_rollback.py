"""Rollback: publishing an earlier run keeps later runs from publishing until an analyst
publishes one, so neither the next night nor a re-run from the same cache undoes it."""

import pytest

from pennant.core import db


def test_a_rollback_holds_later_runs_until_one_is_published(conn, run):
    good, bad = run(publish=True).run_id, run(publish=True).run_id
    assert db.rolled_back_from(conn) is None
    db.publish(conn, good)  # the rollback
    assert db.published_run_id(conn) == good and db.rolled_back_from(conn) == bad

    nightly = run(publish=True)  # unattended: staged, not published
    assert nightly.status == "staged" and db.published_run_id(conn) == good
    report = db.get_run(conn, nightly.run_id)["gate_report"]
    assert f"rolled back from {bad} to {good}" in report["paused"]
    after_decision = run(publish=True, author="analyst")  # a re-run after a decision
    assert after_decision.status == "staged" and db.published_run_id(conn) == good

    db.publish(conn, after_decision.run_id)  # an analyst checked it and published it
    assert db.rolled_back_from(conn) is None
    assert run(publish=True).status == "published"


def test_publishing_an_older_run_than_the_rolled_back_one_keeps_the_rollback(conn, run):
    first, second, third = (run(publish=True).run_id for _ in range(3))
    db.publish(conn, first)
    db.publish(conn, second)  # still earlier than the newest published run
    assert db.rolled_back_from(conn) == third
    assert run(publish=True).status == "staged"


def test_runs_without_publish_are_unaffected(conn, run):
    run(publish=True)
    result = run(publish=False)
    assert result.status == "staged"
    assert db.get_run(conn, result.run_id)["gate_report"]["paused"] is None


def test_a_held_run_can_never_be_rolled_back_to(conn, run, world):
    run(publish=True)
    held = db.create_run(conn, "x")
    db.finish_run(conn, held, "held", {})
    with pytest.raises(Exception, match="staged"):
        db.publish(conn, held)
