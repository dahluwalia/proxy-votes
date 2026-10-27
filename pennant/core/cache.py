"""Read-through cache for every external call (LlamaExtract, Companies House, web).

Design choice: one function, `cached()`, in front of every network call, so a
second run is free and gives the same answer. Rows are append-only and the
newest wins. Each row has two dates: `fetched_at`, when it was last updated (its
payload first fetched), and `last_fetched`, the latest fetch that returned the same
payload. A refetch of unchanged data only moves `last_fetched`, so the cache doesn't
fill with duplicates and a changed answer is always a new row. "Unchanged" is exact
equality unless the caller passes `same`, for sources whose answers vary in ways that
don't matter (CH search ranks ties differently on every request); then the stored row,
not the refetch, is what callers get, so a replay sees exactly what is stored. A TTL counts from
`last_fetched`, as does a `fresh_until` cut-off such as 23:59 on the day of the fetch.
Offline mode never calls out: it serves whatever is stored (even
past its TTL, so analyst re-runs stay reproducible) and returns None on a miss,
which callers treat as unknown.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pennant import logs

NAMESPACES = ("llamaextract", "ch", "web")
WEB_TTL = timedelta(days=7)


def cache_key(*parts: Any) -> str:
    """sha256 over the parts. Bytes (e.g. PDF contents) are hashed as-is; everything
    else as canonical JSON. Use for LlamaExtract (pdf bytes, schema version, config)
    and web (prompt version, model, inputs) keys; CH keys are just the URL."""
    h = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            h.update(b"b:" + hashlib.sha256(part).digest())
        else:
            h.update(b"j:" + json.dumps(part, sort_keys=True, default=str).encode())
        h.update(b"\x00")
    return h.hexdigest()


def _newest(conn: sqlite3.Connection, namespace: str, key: str):
    return conn.execute(
        "SELECT id, payload, fetched_at, last_fetched FROM cache WHERE namespace = ? "
        "AND key = ? ORDER BY id DESC LIMIT 1", (namespace, key)).fetchone()


def get_cache(
    conn: sqlite3.Connection, namespace: str, key: str
) -> tuple[Any, datetime] | None:
    """The newest (payload, last updated) for this key, or None."""
    row = _newest(conn, namespace, key)
    if row is None:
        return None
    return json.loads(row[1]), datetime.fromisoformat(row[2])


def cache_dates(
    conn: sqlite3.Connection, namespace: str, key: str
) -> tuple[datetime, datetime] | None:
    """(last updated, last fetched) for this key: when its current payload first arrived,
    and the latest fetch that returned it. None if nothing is stored."""
    row = _newest(conn, namespace, key)
    if row is None:
        return None
    updated = datetime.fromisoformat(row[2])
    return updated, datetime.fromisoformat(row[3]) if row[3] else updated


def put_cache(
    conn: sqlite3.Connection,
    namespace: str,
    key: str,
    payload: Any,
    fetched_at: datetime | None = None,
) -> None:
    """Append a cache row. Also used to seed the cache from recorded fixtures."""
    if namespace not in NAMESPACES:
        raise ValueError(f"unknown cache namespace: {namespace!r}")
    stamp = (fetched_at or datetime.now(UTC)).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO cache (namespace, key, payload, fetched_at, last_fetched) "
            "VALUES (?, ?, ?, ?, ?)",
            (namespace, key, json.dumps(payload, sort_keys=True), stamp, stamp),
        )


def cached(
    conn: sqlite3.Connection,
    namespace: str,
    key: str,
    fetch_fn: Callable[[], Any],
    ttl: timedelta | None = None,
    offline: bool = False,
    fresh_until: Callable[[datetime], datetime] | None = None,
    same: Callable[[Any, Any], bool] | None = None,
) -> Any:
    """Return the stored payload, or call `fetch_fn()` and store its result.

    - A hit within `ttl` of its last fetch, or before `fresh_until(last fetch)` (e.g. 23:59
      that day), never calls fetch_fn; with neither, any hit is final.
    - A refetch that returns the stored payload unchanged only moves its last_fetched date
      and returns the stored payload; a different payload is appended as the new newest
      row. `same(stored, fetched)` decides "unchanged" (default: equal JSON).
    - Offline: return the newest stored payload regardless of ttl, else None.
    - fetch_fn must return JSON-serialisable data. If it raises, nothing is stored
      and the error propagates; if it returns None, nothing is stored. So return
      None or raise for failures you don't want cached (e.g. a CH outage).
    """
    if namespace not in NAMESPACES:
        raise ValueError(f"unknown cache namespace: {namespace!r}")
    row = _newest(conn, namespace, key)
    if row is not None:
        checked, now = _aware(datetime.fromisoformat(row[3] or row[2])), datetime.now(UTC)
        fresh = (now <= fresh_until(checked) if fresh_until
                 else ttl is None or now - checked <= ttl)
        if offline or fresh:
            logs.count(f"cache.{namespace}.hit")
            return json.loads(row[1])
    if offline:
        logs.count(f"cache.{namespace}.offline_miss")
        return None
    logs.count(f"cache.{namespace}.fetched")
    payload = fetch_fn()
    if payload is None:
        return None
    if row is not None and (same(json.loads(row[1]), payload) if same
                            else json.dumps(payload, sort_keys=True) == row[1]):
        logs.count(f"cache.{namespace}.unchanged")
        with conn:  # same answer: record the check, keep the row and its last-updated date
            conn.execute("UPDATE cache SET last_fetched = ? WHERE id = ?",
                         (datetime.now(UTC).isoformat(timespec="seconds"), row[0]))
        return json.loads(row[1])
    logs.count(f"cache.{namespace}.new" if row is None else f"cache.{namespace}.changed")
    put_cache(conn, namespace, key, payload)
    return payload


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
