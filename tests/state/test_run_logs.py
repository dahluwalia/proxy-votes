"""Each pipeline run writes its own log: stage times and counts, errors with tracebacks."""

from __future__ import annotations

from typing import Any

from pennant.core import db


def _report(conn_, run_id: str) -> dict[str, Any]:
    return db.get_run(conn_, run_id)["gate_report"]


def test_each_run_writes_its_own_log_with_stage_times_and_counts(conn, run):
    result = run()
    report = _report(conn, result.run_id)
    text = open(report["log"], encoding="utf-8").read()
    assert report["log"].endswith("memory-run-1.log")
    assert f"{result.run_id} started by system (offline" in text
    assert "stage extract: started" in text and "stage score: ok in" in text
    assert f"{result.run_id} finished: staged" in text
    assert set(report["timings"]) == {s.stage for s in result.stages}
    assert isinstance(report["metrics"], dict)


def test_a_stage_error_is_logged_with_its_traceback(conn, run, world):
    world.boom = "dedup"
    result = run()
    text = open(_report(conn, result.run_id)["log"], encoding="utf-8").read()
    assert "ERROR" in text and "stage dedup: failed" in text and "Traceback" in text
    assert f"{result.run_id} finished: failed" in text


def test_two_runs_never_share_a_log(conn, run):
    a, b = run(), run()
    assert _report(conn, a.run_id)["log"] != _report(conn, b.run_id)["log"]
