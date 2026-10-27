"""Run logging: one log file per run, and counters for the work each run does.

Design choice: stdlib logging under the "pennant" logger, plus one Counter of named
metrics (PDF pages read, Companies House requests and pages, cache hits, AI tokens and
LlamaExtract credits). The pipeline opens a log file per run (`logs/<db>-<run>.log` next
to the database, or in `$PENNANT_LOG_DIR`), logs each stage's start, end, time and
counts, every error with its traceback, and saves the counters with the run's report, so
`pennant log` shows them later. AI usage is counted twice over: `live` is what this run
spent (calls it made), and `cached` is what the answers it reused cost when they were
first fetched. The counters are a plain module-level Counter, reset at the start of each
run: runs are one at a time in one process (no async).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pennant.core.config import ROOT

LOGGER = logging.getLogger("pennant")
METRICS: Counter[str] = Counter()
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def get(name: str) -> logging.Logger:
    """The logger for one module: `pennant.<name>`."""
    return logging.getLogger(f"pennant.{name}")


def count(name: str, n: int | float = 1) -> None:
    METRICS[name] += n


def reset() -> None:
    METRICS.clear()


def snapshot() -> dict[str, int | float]:
    return dict(sorted(METRICS.items()))


def log_path(conn: sqlite3.Connection, run_id: str) -> Path:
    """`logs/<database>-<run>.log` next to the run's database (pennant.db's run:20 is
    `logs/pennant-run-20.log`), so two databases' run:1 never share a file.
    `$PENNANT_LOG_DIR` overrides the folder."""
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    stem = Path(db_file).stem if db_file else "memory"
    directory = os.environ.get("PENNANT_LOG_DIR") or (
        Path(db_file).parent / "logs" if db_file else ROOT / "logs")
    return Path(directory) / f"{stem}-{run_id.replace(':', '-')}.log"


def ai_usage(kind: str, usage: dict[str, Any] | None, live: bool) -> None:
    """Count one research model answer's tokens and searches under `ai.<kind>`."""
    where = "live" if live else "cached"
    usage = usage or {}
    count(f"ai.{kind}.calls_{where}")
    for field, name in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                        ("web_search_requests", "web_searches")):
        count(f"ai.{kind}.{name}_{where}", int(usage.get(field) or 0))


def extract_usage(usage: dict[str, Any] | None, live: bool) -> None:
    """Count one LlamaExtract job's credits (it reports credits, not tokens)."""
    where = "live" if live else "cached"
    count(f"llamaextract.jobs_{where}")
    count(f"llamaextract.credits_{where}", float((usage or {}).get("credits") or 0))


@contextmanager
def run_log(path: Path) -> Iterator[Path]:
    """Send the "pennant" logger's records to this run's own file while the block runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter(FORMAT, "%Y-%m-%d %H:%M:%S"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    try:
        yield path
    finally:
        LOGGER.removeHandler(handler)
        handler.close()


def ai_totals(metrics: dict[str, Any]) -> dict[str, dict[str, int]]:
    """{'live'|'cached': {calls, input_tokens, output_tokens, web_searches}} over every kind."""
    out: dict[str, dict[str, int]] = {"live": Counter(), "cached": Counter()}
    for name, value in metrics.items():
        parts = name.split(".")
        if parts[0] == "ai" and len(parts) == 3:
            field, _, where = parts[2].rpartition("_")
            out.setdefault(where, Counter())[field] += value
    return {k: dict(v) for k, v in out.items()}
