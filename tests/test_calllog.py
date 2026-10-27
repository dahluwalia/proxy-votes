"""The model call record: every live research call and LlamaExtract job, with its prompt,
response, outcome, latency, usage and what validation kept; append-only; summarised,
shown and exported by `pennant calls`."""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import anthropic
import httpx2
import pytest
from typer.testing import CliRunner

from pennant import calllog
from pennant.classify import backends, websearch
from pennant.core import db
from pennant.core.cache import put_cache
from pennant.extract import llamaextract as le
from tests.classify.test_research import REQUEST, fact, payload
from tests.extract.test_llamaextract import TESCO, FakeClient

ANSWER = {"equity_listed": fact("yes"), "exchange": fact("unknown", None, None),
          "ticker": fact("TSCO", "https://elsewhere.example/tsco", "TSCO")}


def _live(ctx, monkeypatch, result):
    def fake_call(settings, prompt, meta, domains=None):
        if isinstance(result, Exception):
            raise result
        return {**result, **meta}

    monkeypatch.setattr(websearch, "call", fake_call)
    return dataclasses.replace(ctx, offline=False)


def test_a_live_research_call_is_recorded_in_full(ctx, monkeypatch):
    p = payload(ANSWER)
    p["usage"] = {"input_tokens": 900, "output_tokens": 40, "web_search_requests": 1}
    live = _live(ctx, monkeypatch, p)
    websearch.ask("org_v1", {"org_key": "tesco"}, lambda: "Is Tesco listed?", live, kind="org")
    websearch.ask("org_v1", {"org_key": "tesco"}, lambda: "Is Tesco listed?", live, kind="org")
    [c] = calllog.rows(ctx.conn)  # the second answer came from the cache: no call
    assert (c["run_id"], c["kind"], c["prompt_version"], c["outcome"]) == (
        ctx.run_id, "org", "org_v1", "ok")
    assert c["prompt"] == "Is Tesco listed?" and c["inputs"] == {"org_key": "tesco"}
    assert c["usage"]["input_tokens"] == 900 and c["latency_s"] is not None
    assert c["validation"]["kept"] == ["equity_listed"]
    assert "not a returned search result" in c["validation"]["rejected"][0]  # the ticker
    assert any(b.get("type") == "text" for b in c["response"]["content"])


def test_an_unusable_answer_is_recorded_as_rejected(ctx, monkeypatch):
    p = payload(ANSWER)
    p["content"] = [b for b in p["content"] if b["type"] != "text"]  # no JSON answer
    websearch.ask("org_v1", {"org_key": "x"}, lambda: "q", _live(ctx, monkeypatch, p), kind="org")
    [c] = calllog.rows(ctx.conn)
    assert (c["outcome"], c["error"]) == ("rejected", "no JSON answer")


def test_outages_and_errors_are_recorded_too(ctx, monkeypatch):
    live = _live(ctx, monkeypatch, backends.ModelUnavailable("RateLimitError"))
    websearch.ask("org_v1", {"org_key": "x"}, lambda: "q", live, kind="org")
    auth = anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=REQUEST),
                                         body=None)
    live = _live(ctx, monkeypatch, auth)
    with pytest.raises(anthropic.AuthenticationError):
        websearch.ask("org_v1", {"org_key": "y"}, lambda: "q", live, kind="org")
    assert [c["outcome"] for c in calllog.rows(ctx.conn)] == ["unavailable", "error"]


def test_a_llamaextract_job_is_recorded_with_its_prompt_and_credits(ctx, tmp_path):
    live = dataclasses.replace(ctx, offline=False)
    le.extract_notice(TESCO, live, client=FakeClient(), fixtures_dir=tmp_path)
    [c] = calllog.rows(ctx.conn, kind="extract")
    assert (c["provider"], c["outcome"], c["usage"]) == ("llamaextract", "ok", {"credits": 400.0})
    assert c["prompt"] == le.SYSTEM_PROMPT and c["inputs"]["pdf"] == TESCO.name
    assert c["inputs"]["tier"] == "agentic" and c["response"]["status"] == "COMPLETED"


def test_a_failed_llamaextract_job_is_recorded(ctx, tmp_path):
    live = dataclasses.replace(ctx, offline=False)
    with pytest.raises(le.ExtractError):
        le.extract_notice(TESCO, live, client=FakeClient(fail_upload=True), fixtures_dir=tmp_path)
    [c] = calllog.rows(ctx.conn)
    assert c["outcome"] == "error" and "network down" in c["error"]


def test_the_record_is_append_only(ctx):
    calllog.record(ctx.conn, run_id="run:1", provider="anthropic", kind="org", inputs={},
                   outcome="ok")
    for sql in ("UPDATE model_calls SET outcome = 'error'", "DELETE FROM model_calls"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ctx.conn.execute(sql)


def test_backfill_copies_cached_answers_once(ctx):
    p = payload(ANSWER)
    put_cache(ctx.conn, "web", "k1", {**p, "kind": "org", "prompt_version": "org_v1",
                                      "inputs": {"org_key": "tesco"}})
    assert calllog.backfill_from_cache(ctx.conn) == 1
    assert calllog.backfill_from_cache(ctx.conn) == 0
    [c] = calllog.rows(ctx.conn)
    assert c["prompt"] is None and c["validation"]["kept"] == ["equity_listed"]


def test_pennant_calls_summarises_shows_and_exports(tmp_path, monkeypatch):
    import pennant.cli as cli_mod

    monkeypatch.setenv("PENNANT_DB", str(tmp_path / "x.db"))
    conn = db.connect(tmp_path / "x.db")
    for outcome in ("ok", "ok", "rejected"):
        calllog.record(conn, run_id="run:1", provider="anthropic", kind="site",
                       prompt_version="site_v1", model="m", inputs={"org_key": "a"},
                       prompt="Find the site", outcome=outcome, latency_s=2.0,
                       usage={"input_tokens": 100, "output_tokens": 10},
                       response={"content": [{"type": "text", "text": "the answer"}]},
                       validation={"kept": ["website"], "rejected": []})
    conn.close()
    run = CliRunner()
    out = run.invoke(cli_mod.app, ["calls"]).output
    row = next(ln for ln in out.splitlines() if "site_v1" in ln)
    assert row.split()[3:6] == ["3", "2", "1"]  # calls, ok, rejected
    shown = run.invoke(cli_mod.app, ["calls", "--show", "1"]).output
    assert "Find the site" in shown and "the answer" in shown
    run.invoke(cli_mod.app, ["calls", "--export", str(tmp_path / "c.jsonl")])
    lines = (tmp_path / "c.jsonl").read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["prompt"] == "Find the site"
