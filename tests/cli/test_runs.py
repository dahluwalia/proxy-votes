"""`pennant runs`, `pennant rollback` and `pennant publish` on a fixture database."""

from typer.testing import CliRunner

from pennant.cli import app, load_settings
from pennant.core import db

from .conftest import fake_run_pipeline

runner = CliRunner()


def cli(*args, **kwargs):
    return runner.invoke(app, list(args), **kwargs)


def pointer():
    conn = db.connect(load_settings().db_path)
    try:
        return db.published_run_id(conn), db.rolled_back_from(conn)
    finally:
        conn.close()


def test_rollback_then_publish_a_newer_run(cli_db):
    fake_run_pipeline(load_settings(), offline=True, publish=True, author="system")  # run:2
    assert pointer() == ("run:2", None)

    res = cli("rollback", "run:1", "-y")
    assert res.exit_code == 0, res.output
    assert "Published run:1" in res.output and "This is a rollback" in res.output
    assert pointer() == ("run:1", "run:2")
    listed = cli("runs").output
    assert "◀ published" in listed and "rolled back from" in listed
    assert "Rolled back from run:2 to run:1" in listed

    fake_run_pipeline(load_settings(), offline=True, publish=False, author="system")  # run:3
    res = cli("publish", "run:3", "-y")
    assert res.exit_code == 0, res.output
    assert "This ends the rollback" in res.output and pointer() == ("run:3", None)


def test_rollback_refuses_held_runs_newer_runs_and_unknown_runs(cli_db):
    fake_run_pipeline(load_settings(), offline=True, publish=False, author="system",
                      status="held")  # run:2
    assert "only a run that passed its gates" in cli("rollback", "run:2", "-y").output
    fake_run_pipeline(load_settings(), offline=True, publish=False, author="system")  # run:3
    assert "not earlier than" in cli("rollback", "run:3", "-y").output
    assert "No such run" in cli("rollback", "run:9", "-y").output
    assert "already published" in cli("publish", "run:1", "-y").output
    assert pointer() == ("run:1", None)


def test_rollback_asks_first_and_defaults_to_no(cli_db):
    fake_run_pipeline(load_settings(), offline=True, publish=True, author="system")  # run:2
    res = cli("rollback", "run:1", input="\n")
    assert res.exit_code != 0 and "Nothing published" in res.output
    assert pointer() == ("run:2", None)
