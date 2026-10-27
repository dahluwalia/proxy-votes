"""The four sqlite tables: cache, decisions, runs, run_outputs.

Design choice: append-only, enforced by triggers. Cache rows, decisions and run outputs
are only ever inserted ("latest row wins" on read); an UPDATE or DELETE on them aborts,
except a cache row's `last_fetched` date, which a same-content refetch moves forward.
A run's results are never edited, so diff and rollback are just reads. The only mutable
state is a run's status (set once when it finishes) and its `published_seq` stamp: the
published run is the one stamped most recently, and only a staged run (its gates
passed) or a previously published one (a rollback) can be stamped.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pennant.core.models import OUTPUT_MODELS, Decision

RUN_STATUSES = ("running", "staged", "held", "published", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL CHECK (namespace IN ('llamaextract', 'ch', 'web')),
    key TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL,  -- last updated: when this payload was first fetched
    last_fetched TEXT          -- the latest fetch that returned this same payload
);
CREATE INDEX IF NOT EXISTS cache_lookup ON cache (namespace, key, id);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT GENERATED ALWAYS AS ('dec:' || id) VIRTUAL,
    kind TEXT NOT NULL CHECK (kind IN ('must_link', 'cannot_link', 'set_fact', 'revoke')),
    payload TEXT NOT NULL,
    author TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT GENERATED ALWAYS AS ('run:' || id) VIRTUAL,
    status TEXT NOT NULL CHECK (status IN ('running', 'staged', 'held', 'published', 'failed')),
    config_hash TEXT NOT NULL,
    author TEXT NOT NULL,
    gate_report TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    published_at TEXT,
    published_seq INTEGER
);

CREATE TABLE IF NOT EXISTS run_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_outputs_lookup ON run_outputs (run_id, kind, key, id);

-- Append-only, enforced by the database, not just by convention: a decision is undone by a
-- revoke row, a cache entry is superseded by a newer row, a run's results are never edited.
-- The one mutable cell is a cache row's last_fetched: a refetch that returns the same payload
-- records the date it was checked instead of storing a duplicate row.
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only: record a revoke instead'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only: record a revoke instead'); END;
CREATE TRIGGER IF NOT EXISTS run_outputs_no_update BEFORE UPDATE ON run_outputs
BEGIN SELECT RAISE(ABORT, 'run outputs are append-only: start a new run'); END;
CREATE TRIGGER IF NOT EXISTS run_outputs_no_delete BEFORE DELETE ON run_outputs
BEGIN SELECT RAISE(ABORT, 'run outputs are append-only: start a new run'); END;
DROP TRIGGER IF EXISTS cache_no_update;
CREATE TRIGGER IF NOT EXISTS cache_payload_fixed
BEFORE UPDATE OF id, namespace, key, payload, fetched_at ON cache
BEGIN SELECT RAISE(ABORT, 'the cache is append-only: a newer row wins'); END;
CREATE TRIGGER IF NOT EXISTS cache_no_delete BEFORE DELETE ON cache
BEGIN SELECT RAISE(ABORT, 'the cache is append-only: a newer row wins'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'runs are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS runs_identity_fixed
BEFORE UPDATE OF config_hash, author, started_at ON runs
BEGIN SELECT RAISE(ABORT, 'a run''s author, config and start time never change'); END;
-- A run finishes once, and only a run that passed its gates (staged), or one published
-- before (a rollback), can be published.
CREATE TRIGGER IF NOT EXISTS runs_finish_once BEFORE UPDATE OF status ON runs
WHEN OLD.status <> 'running' AND NEW.status <> 'published'
BEGIN SELECT RAISE(ABORT, 'a finished run''s status never changes, except to publish it'); END;
CREATE TRIGGER IF NOT EXISTS runs_publish_gated BEFORE UPDATE OF status ON runs
WHEN NEW.status = 'published' AND OLD.status NOT IN ('staged', 'published')
BEGIN SELECT RAISE(ABORT, 'only a staged run (gates passed) or a published one can be published');
END;
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _int_id(text_id: str, prefix: str) -> int:
    if not text_id.startswith(prefix):
        raise ValueError(f"expected an id like {prefix}3, got {text_id!r}")
    return int(text_id.removeprefix(prefix))


def init_db(conn: sqlite3.Connection) -> None:
    """Create the tables if missing, and add columns a newer schema has. Safe to call any
    number of times."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'cache'").fetchone():
        if "last_fetched" not in {r[1] for r in conn.execute("PRAGMA table_info(cache)")}:
            conn.execute("ALTER TABLE cache ADD COLUMN last_fetched TEXT")
    conn.executescript(SCHEMA)


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


# --- run_outputs -------------------------------------------------------------


def output_key(kind: str, model: BaseModel) -> str:
    """The natural key of each output kind (review items are keyed by content)."""
    m: Any = model
    match kind:
        case "meeting":
            return m.meeting_id
        case "roster":
            return m.mention_id
        case "seat":
            return m.seat_id
        case "person":
            return m.person_id
        case "org":
            return m.org_key
        case "officer":
            return m.officer_id
        case "position":
            return m.seat_id
        case "recommendation":
            return f"{m.meeting_id}|{m.person_id}"
        case "review":
            return f"{m.kind}|{m.subject}|{m.detail}"
    raise ValueError(f"unknown output kind: {kind!r}")


def put_outputs(
    conn: sqlite3.Connection, run_id: str, kind: str, models: Iterable[BaseModel]
) -> int:
    """Append models as this run's outputs of `kind`. Writing a key again (a later stage
    refining a seat, say) adds a newer row; get_outputs returns the newest."""
    if kind not in OUTPUT_MODELS:
        raise ValueError(f"unknown output kind: {kind!r}")
    model_cls = OUTPUT_MODELS[kind]
    rows = []
    for m in models:
        if not isinstance(m, model_cls):
            raise TypeError(f"{kind} outputs must be {model_cls.__name__}, got {type(m).__name__}")
        rows.append((run_id, kind, output_key(kind, m), m.model_dump_json()))
    with conn:
        conn.executemany(
            "INSERT INTO run_outputs (run_id, kind, key, payload) VALUES (?, ?, ?, ?)", rows
        )
    return len(rows)


def get_outputs(conn: sqlite3.Connection, run_id: str, kind: str) -> list[Any]:
    """This run's outputs of `kind` as models, newest row per key, sorted by key."""
    model_cls = OUTPUT_MODELS[kind]
    latest: dict[str, str] = {}
    for row in conn.execute(
        "SELECT key, payload FROM run_outputs WHERE run_id = ? AND kind = ? ORDER BY id",
        (run_id, kind),
    ):
        latest[row["key"]] = row["payload"]
    return [model_cls.model_validate_json(latest[k]) for k in sorted(latest)]


# --- runs --------------------------------------------------------------------


def create_run(conn: sqlite3.Connection, config_hash: str, author: str = "system") -> str:
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (status, config_hash, author, started_at) VALUES (?, ?, ?, ?)",
            ("running", config_hash, author, now_iso()),
        )
    return f"run:{cur.lastrowid}"


def finish_run(
    conn: sqlite3.Connection, run_id: str, status: str, gate_report: dict[str, Any] | None
) -> None:
    """Record a run's outcome: staged, held or failed (use publish() to publish)."""
    if status not in RUN_STATUSES or status in ("running", "published"):
        raise ValueError(f"finish_run status must be staged, held or failed: {status!r}")
    with conn:
        conn.execute(
            "UPDATE runs SET status = ?, gate_report = ?, finished_at = ? WHERE id = ?",
            (status, json.dumps(gate_report, sort_keys=True), now_iso(), _int_id(run_id, "run:")),
        )


def publish(conn: sqlite3.Connection, run_id: str) -> None:
    """Move the published pointer to `run_id` (also used to roll back). Only a staged run
    (its gates passed) or a previously published one can be published; the database
    refuses anything else."""
    with conn:
        conn.execute(
            "UPDATE runs SET status = 'published', published_at = ?, published_seq = "
            "(SELECT COALESCE(MAX(published_seq), 0) + 1 FROM runs) WHERE id = ?",
            (now_iso(), _int_id(run_id, "run:")),
        )


def published_run_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM runs WHERE published_seq IS NOT NULL "
        "ORDER BY published_seq DESC LIMIT 1"
    ).fetchone()
    return row["run_id"] if row else None


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    run = dict(row)
    run["gate_report"] = json.loads(run["gate_report"]) if run["gate_report"] else None
    return run


def list_runs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]


# --- decisions ---------------------------------------------------------------


def insert_decision(
    conn: sqlite3.Connection, kind: str, payload: dict[str, Any], author: str, reason: str
) -> Decision:
    """Append a decision row. Validation (e.g. must-link vs cannot-link) is M7.1's job.
    A revoke's payload is {"decision_id": "dec:<n>"}."""
    with conn:
        cur = conn.execute(
            "INSERT INTO decisions (kind, payload, author, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, json.dumps(payload, sort_keys=True), author, reason, now_iso()),
        )
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _decision(row)


def _decision(row: sqlite3.Row) -> Decision:
    return Decision(
        decision_id=row["decision_id"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        author=row["author"],
        reason=row["reason"],
        created_at=row["created_at"],
    )


def all_decisions(conn: sqlite3.Connection) -> list[Decision]:
    """Every decision, revokes included, in the order they were made."""
    return [_decision(r) for r in conn.execute("SELECT * FROM decisions ORDER BY id")]


def active_decisions(conn: sqlite3.Connection) -> list[Decision]:
    """Non-revoke decisions not hidden by an active revoke, oldest first.
    Revoking a revoke (undo of an undo) brings its target back."""
    decisions = all_decisions(conn)
    revokers: dict[str, list[str]] = {}
    for d in decisions:
        if d.kind == "revoke":
            revokers.setdefault(d.payload["decision_id"], []).append(d.decision_id)
    memo: dict[str, bool] = {}

    def active(did: str) -> bool:
        # Revokes always come after their target, so this recursion terminates.
        if did not in memo:
            memo[did] = not any(active(r) for r in revokers.get(did, []))
        return memo[did]

    return [d for d in decisions if d.kind != "revoke" and active(d.decision_id)]
