"""M0.2: the read-through cache."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from pennant.core import db
from pennant.core.cache import WEB_TTL, cache_dates, cache_key, cached, get_cache, put_cache


def boom():
    raise AssertionError("fetch_fn must not be called")


def test_miss_fetches_and_stores_then_hit_never_calls_fetch(ctx):
    calls = []

    def fetch():
        calls.append(1)
        return {"name": "TESCO PLC"}

    url = "https://api.company-information.service.gov.uk/company/00445790"
    assert cached(ctx.conn, "ch", url, fetch) == {"name": "TESCO PLC"}
    assert cached(ctx.conn, "ch", url, boom) == {"name": "TESCO PLC"}
    assert cached(ctx.conn, "ch", url, boom, offline=True) == {"name": "TESCO PLC"}
    assert calls == [1]


def test_offline_miss_returns_none(ctx):
    assert cached(ctx.conn, "web", "k", boom, offline=True) is None
    assert get_cache(ctx.conn, "web", "k") is None


def test_ttl_expiry_refetches_online_but_not_offline(ctx):
    put_cache(ctx.conn, "web", "k", {"v": 1}, fetched_at=datetime.now(UTC) - timedelta(days=8))
    assert cached(ctx.conn, "web", "k", boom, ttl=WEB_TTL, offline=True) == {"v": 1}
    assert cached(ctx.conn, "web", "k", lambda: {"v": 2}, ttl=WEB_TTL) == {"v": 2}
    assert cached(ctx.conn, "web", "k", boom, ttl=WEB_TTL) == {"v": 2}  # latest row wins


def test_failures_are_not_cached(ctx):
    assert cached(ctx.conn, "ch", "u", lambda: None) is None
    assert get_cache(ctx.conn, "ch", "u") is None

    def fail():
        raise RuntimeError("503")

    with pytest.raises(RuntimeError):
        cached(ctx.conn, "ch", "u", fail)
    assert get_cache(ctx.conn, "ch", "u") is None


def test_unknown_namespace_rejected(ctx):
    with pytest.raises(ValueError):
        cached(ctx.conn, "other", "k", boom)


def test_cache_key_is_stable_and_sensitive():
    k = cache_key(b"%PDF-1.7", "schema-v1", {"tier": "agentic", "cite_sources": True})
    assert k == cache_key(b"%PDF-1.7", "schema-v1", {"cite_sources": True, "tier": "agentic"})
    assert k != cache_key(b"%PDF-1.8", "schema-v1", {"tier": "agentic", "cite_sources": True})
    assert cache_key("a", "b") != cache_key("ab")


def _rows(conn, key):
    return conn.execute("SELECT payload, fetched_at, last_fetched FROM cache WHERE key = ? "
                        "ORDER BY id", (key,)).fetchall()


def test_identical_refetch_moves_last_fetched_and_a_change_is_a_new_row(ctx):
    """Last updated = when the payload first arrived; last fetched = the latest check.
    A refetch with the same answer adds no row; a different answer is appended."""
    conn, day = ctx.conn, timedelta(hours=23, minutes=59)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    put_cache(conn, "ch", "u", {"officers": 1}, fetched_at=old)
    assert cached(conn, "ch", "u", lambda: {"officers": 1}, ttl=day) == {"officers": 1}
    (row,) = _rows(conn, "u")
    assert row[1] == old.isoformat() and row[2] > row[1]  # updated stays, fetched moves
    updated, fetched = cache_dates(conn, "ch", "u")
    assert updated == old and fetched > old
    assert cached(conn, "ch", "u", boom, ttl=day) == {"officers": 1}  # fresh: no refetch
    conn.execute("UPDATE cache SET last_fetched = ?", (old.isoformat(),))
    assert cached(conn, "ch", "u", lambda: {"officers": 2}, ttl=day) == {"officers": 2}
    assert [r[0] for r in _rows(conn, "u")] == ['{"officers": 1}', '{"officers": 2}']
    assert get_cache(conn, "ch", "u")[0] == {"officers": 2}


def test_only_last_fetched_can_change_on_a_cache_row(ctx):
    put_cache(ctx.conn, "ch", "u", {"a": 1})
    with ctx.conn:
        ctx.conn.execute("UPDATE cache SET last_fetched = '2026-12-31T00:00:00+00:00'")
    for sql in ("UPDATE cache SET payload = '{}'", "UPDATE cache SET fetched_at = 'x'",
                "UPDATE cache SET key = 'v'", "DELETE FROM cache"):
        with pytest.raises(sqlite3.IntegrityError):
            with ctx.conn:
                ctx.conn.execute(sql)


def test_a_database_made_before_last_fetched_is_migrated(tmp_path):
    """An older pennant.db (no last_fetched column, the old blanket trigger) opens cleanly:
    the column is added, old rows read their last-updated date as last fetched."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE cache (id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL,
            key TEXT NOT NULL, payload TEXT NOT NULL, fetched_at TEXT NOT NULL);
        CREATE TRIGGER cache_no_update BEFORE UPDATE ON cache
        BEGIN SELECT RAISE(ABORT, 'old'); END;
        INSERT INTO cache (namespace, key, payload, fetched_at)
        VALUES ('ch', 'u', '{"a": 1}', '2026-01-01T00:00:00+00:00');""")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert cache_dates(conn, "ch", "u") == (datetime(2026, 1, 1, tzinfo=UTC),) * 2
    assert cached(conn, "ch", "u", lambda: {"a": 1}, ttl=timedelta(hours=1)) == {"a": 1}
    assert cache_dates(conn, "ch", "u")[1] > datetime(2026, 1, 1, tzinfo=UTC)


def test_custom_sameness_keeps_the_stored_row_and_returns_it(ctx):
    """A source whose answers vary in ways that don't matter (CH search order) passes
    `same`; the stored payload, not the refetch, is what the caller gets."""
    put_cache(ctx.conn, "ch", "s", {"items": [1, 2]}, fetched_at=datetime(2026, 1, 1, tzinfo=UTC))
    same = lambda a, b: sorted(a["items"]) == sorted(b["items"])  # noqa: E731
    got = cached(ctx.conn, "ch", "s", lambda: {"items": [2, 1]}, ttl=timedelta(0), same=same)
    assert got == {"items": [1, 2]} and len(_rows(ctx.conn, "s")) == 1
    got = cached(ctx.conn, "ch", "s", lambda: {"items": [3]}, ttl=timedelta(0), same=same)
    assert got == {"items": [3]} and len(_rows(ctx.conn, "s")) == 2
