"""A record of every live model call, for analysing and tuning prompts and models later.

Design choice: the cache keeps one answer per question, not the prompt that produced it,
how long it took, or the calls that failed and were never cached. So every live call to
the research model or LlamaExtract is also appended to its own table, `model_calls`: the
run, the question kind and inputs, the prompt version and the exact prompt text, the model,
the full response, the outcome (`ok`, `rejected` for an answer that came back unusable,
`unavailable`, `error`), latency, usage (tokens, searches, credits) and, for research, what
validation kept and dropped and why. Rows are append-only (triggers refuse edits), like
the rest of the database. Recording never breaks a run: a failure to write is logged and
the call's result is returned as usual. `pennant calls` summarises and exports the rows;
`--backfill` copies in answers cached before this record existed (no prompt text).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pennant import logs

log = logs.get("calls")
OUTCOMES = ("ok", "rejected", "unavailable", "error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    called_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    prompt_version TEXT,
    model TEXT,
    inputs TEXT NOT NULL,
    prompt TEXT,
    response TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'rejected', 'unavailable', 'error')),
    error TEXT,
    latency_s REAL,
    usage TEXT,
    validation TEXT,
    cache_key TEXT
);
CREATE INDEX IF NOT EXISTS model_calls_kind ON model_calls (kind, prompt_version, model);
CREATE TRIGGER IF NOT EXISTS model_calls_no_update BEFORE UPDATE ON model_calls
BEGIN SELECT RAISE(ABORT, 'model_calls is append-only'); END;
CREATE TRIGGER IF NOT EXISTS model_calls_no_delete BEFORE DELETE ON model_calls
BEGIN SELECT RAISE(ABORT, 'model_calls is append-only'); END;
"""
JSON_FIELDS = ("inputs", "response", "usage", "validation")


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def record(conn: sqlite3.Connection, *, run_id: str | None, provider: str, kind: str,
           inputs: dict[str, Any], outcome: str, prompt_version: str | None = None,
           model: str | None = None, prompt: str | None = None, response: Any = None,
           error: str | None = None, latency_s: float | None = None,
           usage: dict[str, Any] | None = None, validation: dict[str, Any] | None = None,
           cache_key: str | None = None, called_at: str | None = None) -> None:
    """Append one call. Never raises: telemetry must not fail a run."""
    def as_json(v: Any) -> str | None:
        return None if v is None else json.dumps(v, sort_keys=True, ensure_ascii=False)

    row = {"run_id": run_id,
           "called_at": called_at or datetime.now(UTC).isoformat(timespec="seconds"),
           "provider": provider, "kind": kind, "prompt_version": prompt_version,
           "model": model, "inputs": as_json(inputs), "prompt": prompt,
           "response": as_json(response), "outcome": outcome, "error": error,
           "latency_s": None if latency_s is None else round(latency_s, 2),
           "usage": as_json(usage), "validation": as_json(validation), "cache_key": cache_key}
    try:
        ensure(conn)
        with conn:
            conn.execute(f"INSERT INTO model_calls ({', '.join(row)}) "
                         f"VALUES ({', '.join('?' * len(row))})", list(row.values()))
    except Exception:
        log.exception("could not record a %s %s call", provider, kind)


def rows(conn: sqlite3.Connection, run_id: str | None = None,
         kind: str | None = None) -> list[dict[str, Any]]:
    """Recorded calls, oldest first, with JSON fields decoded."""
    ensure(conn)
    sql, args = "SELECT * FROM model_calls WHERE 1 = 1", []
    if run_id:
        sql, args = sql + " AND run_id = ?", [*args, run_id]
    if kind:
        sql, args = sql + " AND kind = ?", [*args, kind]
    out = []
    for r in conn.execute(sql + " ORDER BY id", args):
        d = dict(r)
        out.append({k: (json.loads(v) if k in JSON_FIELDS and v else v) for k, v in d.items()})
    return out


def summary(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per (kind, prompt version, model): calls by outcome, mean latency, mean tokens or
    credits, and facts validation kept and dropped."""
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for c in calls:
        groups[(c["kind"], c["prompt_version"] or "-", c["model"] or "-")].append(c)
    out = []
    for (kind, version, model), cs in sorted(groups.items()):
        n = len(cs)
        usage = [c["usage"] or {} for c in cs]
        val = [c["validation"] or {} for c in cs]
        lat = [c["latency_s"] for c in cs if c["latency_s"] is not None]
        out.append({
            "kind": kind, "prompt_version": version, "model": model, "calls": n,
            **{o: sum(c["outcome"] == o for c in cs) for o in OUTCOMES},
            "mean_latency_s": round(sum(lat) / len(lat), 1) if lat else None,
            "mean_input_tokens": round(sum(u.get("input_tokens") or 0 for u in usage) / n),
            "mean_output_tokens": round(sum(u.get("output_tokens") or 0 for u in usage) / n),
            "mean_searches": round(sum(u.get("web_search_requests") or 0 for u in usage) / n, 1),
            "credits": sum(float(u.get("credits") or 0) for u in usage),
            "facts_kept": sum(len(v.get("kept") or []) for v in val),
            "facts_dropped": sum(len(v.get("rejected") or []) for v in val),
        })
    return out


def export_jsonl(calls: list[dict[str, Any]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in calls:
            f.write(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n")
    return len(calls)


def backfill_from_cache(conn: sqlite3.Connection) -> int:
    """Copy cached answers fetched before this record existed into model_calls, once each
    (by cache key): the response, model, usage and fetch time are in the cache, the prompt
    text is not, so `prompt` stays empty and `run_id` is None. Returns rows added."""
    from pennant.classify.websearch import parse_answer, validate  # it records via this module

    ensure(conn)
    have = {k for (k,) in conn.execute("SELECT cache_key FROM model_calls")}
    added = 0
    for namespace, key, raw, fetched in conn.execute(
            "SELECT namespace, key, payload, fetched_at FROM cache WHERE namespace IN "
            "('web', 'llamaextract') ORDER BY id").fetchall():
        if key in have:
            continue
        have.add(key)
        p = json.loads(raw)
        common = dict(run_id=None, outcome="ok", cache_key=key, called_at=fetched,
                      usage=p.get("usage"), error="backfilled from the cache: no prompt text")
        if namespace == "web":
            domain = (p.get("inputs") or {}).get("domain")
            values, _, rejected = validate(p, domain)
            record(conn, **common, provider="anthropic", kind=p.get("kind", "?"),
                   prompt_version=p.get("prompt_version"), model=p.get("model"),
                   inputs=p.get("inputs") or {},
                   response={k: p.get(k) for k in ("model", "stop_reason", "content")},
                   validation={"kept": sorted(values), "rejected": rejected,
                               "answer": parse_answer(p), "domains": [domain] if domain else None})
        else:
            record(conn, **common, provider="llamaextract", kind="extract",
                   prompt_version="system_v1", model=(p.get("configuration") or {}).get("tier"),
                   inputs={"job_id": p.get("id")}, response=p)
        added += 1
    log.info("backfilled %d model calls from the cache", added)
    return added
