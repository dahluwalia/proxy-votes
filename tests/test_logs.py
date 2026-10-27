"""Run logging: a log file per run, stage times and counts, errors with tracebacks, and
counters for pages, CH requests, cache use, AI tokens and LlamaExtract credits."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from typer.testing import CliRunner

from pennant import logs
from pennant.classify import websearch
from pennant.core import db
from pennant.core.cache import cached, put_cache
from tests.classify.test_research import fact, payload


def test_cache_counts_hits_fetches_and_unchanged_refetches(ctx):
    logs.reset()
    cached(ctx.conn, "ch", "u1", lambda: {"a": 1})  # new
    cached(ctx.conn, "ch", "u1", lambda: {"a": 1})  # hit (no ttl: any hit is final)
    put_cache(ctx.conn, "ch", "u2", {"b": 1})
    cached(ctx.conn, "ch", "u2", lambda: {"b": 1}, ttl=timedelta(0))  # stale, same answer
    cached(ctx.conn, "ch", "u3", lambda: {"c": 1}, offline=True)  # offline miss
    m = logs.snapshot()
    assert (m["cache.ch.new"], m["cache.ch.hit"], m["cache.ch.unchanged"],
            m["cache.ch.offline_miss"], m["cache.ch.fetched"]) == (1, 1, 1, 1, 2)


def test_ai_tokens_are_counted_live_once_then_cached(ctx, monkeypatch):
    """A live answer counts as spent this run; the same answer from the cache counts as
    reused, with what it cost when fetched."""
    def fake_call(settings, prompt, meta, domains=None):
        p = payload({"equity_listed": fact("yes")})
        p["usage"] = {"input_tokens": 1000, "output_tokens": 50, "web_search_requests": 2}
        return {**p, **meta}

    monkeypatch.setattr(websearch, "call", fake_call)
    live = dataclasses.replace(ctx, offline=False)
    logs.reset()
    websearch.ask("org_v1", {"org_key": "x"}, lambda: "q", live, kind="org")
    websearch.ask("org_v1", {"org_key": "x"}, lambda: "q", live, kind="org")
    totals = logs.ai_totals(logs.snapshot())
    assert totals["live"] == {"calls": 1, "input_tokens": 1000, "output_tokens": 50,
                              "web_searches": 2}
    assert totals["cached"]["calls"] == 1 and totals["cached"]["input_tokens"] == 1000


def test_llamaextract_credits_are_counted():
    logs.reset()
    logs.extract_usage({"credits": 400.0}, live=True)
    logs.extract_usage({"credits": 250.0}, live=False)
    m = logs.snapshot()
    assert (m["llamaextract.credits_live"], m["llamaextract.credits_cached"]) == (400.0, 250.0)


def test_pennant_log_shows_stages_work_and_ai_tokens(tmp_path, monkeypatch):
    import pennant.cli as cli_mod

    path = tmp_path / "x.db"
    monkeypatch.setenv("PENNANT_DB", str(path))
    c = db.connect(path)
    run_id = db.create_run(c, "h")
    log_file = tmp_path / "x-run-1.log"
    log_file.write_text("2026-10-27 10:00:00 INFO    pennant.pipeline: run:1 started\n")
    db.finish_run(c, run_id, "staged", {
        "stages": [{"stage": "extract", "ok": True, "counts": {"meetings": 5}, "errors": []}],
        "timings": {"extract": 1.5}, "log": str(log_file),
        "metrics": {"pdf.pages_read": 120, "ch.requests": 30, "ai.org.calls_live": 2,
                    "ai.org.input_tokens_live": 5000, "ai.org.output_tokens_live": 300,
                    "ai.org.web_searches_live": 4}})
    c.close()
    out = CliRunner().invoke(cli_mod.app, ["log", "--full"]).output
    assert "run:1: staged" in out and "stage extract" in out and "1.5s" in out
    assert "pages_read=120" in out and "requests=30" in out
    assert "org (live)" in out and "5,000" in out
    assert "pennant.pipeline: run:1 started" in out
