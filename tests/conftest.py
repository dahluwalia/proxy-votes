"""Shared pytest scaffold: the `live` marker and an offline, in-memory `ctx`.

Design choice: every unit test runs against a fresh in-memory database with no
API keys and offline=True, so nothing can reach the network by accident. Tests
that do call CH, LlamaExtract or Claude must carry @pytest.mark.live.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pennant.core import db
from pennant.core.config import FIXTURES_DIR, Settings, config_hash, load_policy
from pennant.core.models import RunContext


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: calls Companies House, LlamaExtract or Claude (needs keys in .env)"
    )


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def ctx() -> Iterator[RunContext]:
    """A RunContext on a fresh in-memory database, offline, with a run already created."""
    conn = db.connect(":memory:")
    settings = Settings(db_path=":memory:", offline=True)
    policy = load_policy()
    run_id = db.create_run(conn, config_hash(policy, settings), author="test")
    yield RunContext(conn=conn, run_id=run_id, settings=settings, policy=policy, offline=True)
    conn.close()


@pytest.fixture(autouse=True)
def _run_logs_in_tmp(tmp_path, monkeypatch) -> None:
    """Run logs go to a temporary folder, never the repo's logs/."""
    monkeypatch.setenv("PENNANT_LOG_DIR", str(tmp_path / "logs"))
