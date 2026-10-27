"""M8.1: CliRunner tests on a fixture database, with M7 faked (see conftest.py)."""

import re

from typer.testing import CliRunner

from pennant.cli import app
from pennant.core import db

runner = CliRunner()


def cli(*args):
    return runner.invoke(app, list(args))


def published(path, kind):
    conn = db.connect(path)
    try:
        return db.get_outputs(conn, db.published_run_id(conn), kind)
    finally:
        conn.close()


def table_rows(output):
    return [ln for ln in output.splitlines() if re.match(r"^\d+\s{2}", ln)]


def test_votes_for_tesco_shows_11_rows(cli_db):
    res = cli("votes", "--meeting", "tesco")
    assert res.exit_code == 0, res.output
    rows = table_rows(res.output)
    assert len(rows) == 11
    assert "Tesco PLC — AGM 2026-06-18" in res.output and "ITV" not in res.output
    garnier = next(r for r in rows if "Thierry Garnier" in r)
    assert "AGAINST" in garnier and "exec" in garnier
    silver = next(r for r in rows if "Caroline Silver" in r)
    assert "4–5" in silver and "REVIEW" in silver and "points?" in silver


def test_votes_without_meeting_shows_every_meeting_and_falls_back_on_number(cli_db):
    res = cli("votes")
    assert res.exit_code == 0, res.output
    assert res.output.index("ITV plc") < res.output.index("Tesco PLC")  # by AGM date
    assert len(table_rows(res.output)) == 14
    itv = cli("votes", "--meeting", "itv")  # the ITV meeting has no key; found by name
    assert len(table_rows(itv.output)) == 3
    by_number = cli("votes", "--meeting", "4967001")  # unpadded company number
    assert table_rows(by_number.output) == table_rows(itv.output)


def test_votes_rejects_unknown_meeting(cli_db):
    res = cli("votes", "--meeting", "acme")
    assert res.exit_code != 0


def test_merge_then_undo_restores_identical_recommendations(cli_db):
    before = published(cli_db, "recommendation")
    res = cli("merge", "Ken Murphy", "Gerry Murphy", "--reason", "test", "-y")
    assert res.exit_code == 0, res.output
    assert "Recorded dec:1: must_link" in res.output
    merged = published(cli_db, "recommendation")
    assert merged != before
    assert len({r.person_id for r in merged}) == len({r.person_id for r in before}) - 1
    res = cli("undo", "dec:1", "-y")
    assert res.exit_code == 0, res.output
    assert "Recorded dec:2: revoke dec:1" in res.output
    assert "decision dec:1 no longer applied" in res.output
    assert published(cli_db, "recommendation") == before


def test_split_chris_kennedy_yields_two_people_and_diff_names_the_decision(cli_db):
    people = [p for p in published(cli_db, "person") if "nm:00445790:r9" in p.mention_ids]
    assert people[0].mention_ids == ["nm:00445790:r9", "nm:04967001:r12"]
    res = cli("split", "Chris Kennedy", "--move", "itv", "--reason", "different DOB", "-y")
    assert res.exit_code == 0, res.output
    assert "Recorded dec:1: cannot_link" in res.output
    assert "decision dec:1 applied" in res.output
    after = {p.person_id: p.mention_ids for p in published(cli_db, "person")}
    assert after["p:nm:00445790:r9"] == ["nm:00445790:r9"]
    assert after["p:nm:04967001:r12"] == ["nm:04967001:r12"]
    show = cli("show", "Chris Kennedy")
    assert "ambiguous" in show.output  # now two people share the name
    assert "p:nm:04967001:r12" in show.output


def test_split_payload_is_one_cannot_link(cli_db):
    cli("split", "p:nm:00445790:r9", "--move", "nm:04967001:r12", "--reason", "x", "-y")
    conn = db.connect(cli_db)
    (d,) = db.active_decisions(conn)
    assert d.kind == "cannot_link"
    assert d.payload == {"a": ["nm:04967001:r12"], "b": ["nm:00445790:r9"]}


def test_set_fact_moves_silver_to_for_and_undo_restores_review(cli_db):
    res = cli("set-fact", "seat:silver:moelis", "board_seat", "no", "--reason", "adviser only",
              "-y")
    assert res.exit_code == 0, res.output
    assert "decision dec:1 applied" in res.output
    assert "REVIEW -> FOR" in res.output
    rows = table_rows(cli("votes", "--meeting", "tesco").output)
    assert "FOR" in next(r for r in rows if "Caroline Silver" in r)
    assert 'Analyst (dec:1): "adviser only"' in cli("show", "Caroline Silver").output
    res = cli("undo", "dec:1", "-y")
    assert "FOR -> REVIEW" in res.output


def test_set_fact_validates_subject_field_and_value(cli_db):
    assert "Did you mean: seat:silver:moelis" in cli("set-fact", "silver:moel", "board_seat",
                                                     "no", "-r", "x").output
    assert "seats at moelis: seat:silver:moelis" in cli("set-fact", "moelis", "board_seat", "no",
                                                        "-r", "x").output
    for bad in (["colour", "no"], ["board_seat", "maybe"]):
        res = cli("set-fact", "seat:silver:moelis", *bad, "-r", "x", "-y")
        assert res.exit_code == 1 and "Refused" in res.output
    assert cli("set-fact", "ice", "listed", "no", "-r", "x", "-y").exit_code == 0  # org fact


def test_show_lists_mentions_seats_citations_and_flip_facts(cli_db):
    res = cli("show", "caroline silver")
    assert res.exit_code == 0, res.output
    out = res.output
    assert "Caroline Silver  (p:nm:00445790:r13)" in out
    assert 'Tesco notice p.10: "Chair: Barratt Redrow plc and ICE Clear Europe"' in out
    assert "Web: ICE investors https://ir.theice.com/" in out
    assert "Vote at Tesco: REVIEW" in out
    assert "Would flip the vote: seat:silver:moelis.board_seat" in out
    assert "pennant set-fact seat:silver:moelis board_seat no" in out  # its review item


def test_show_one_person_across_two_notices(cli_db):
    out = cli("show", "Chris Kennedy").output
    assert "Tesco r9" in out and "ITV r12" in out
    assert "off-kennedy  KENNEDY, Christopher, born 3/1964" in out


def test_show_marks_the_citations_the_run_flagged_as_stale(cli_db):
    from pennant.core.models import ReviewItem

    conn = db.connect(cli_db)
    db.put_outputs(conn, db.published_run_id(conn), "review", [ReviewItem(
        kind="stale_evidence", subject="ice",
        detail="Web https://ir.theice.com/ last checked 2026-05-01, before the AGM on "
        "2026-06-18; facts from it may be out of date", suggested_command="pennant run")])
    conn.close()
    out = cli("show", "caroline silver").output
    ice = next(ln for ln in out.splitlines() if "Web: ICE investors https://ir.theice.com/" in ln)
    assert ice.endswith("[STALE]")
    assert "[stale_evidence] ice: Web https://ir.theice.com/ last checked 2026-05-01" in out
    assert out.count("[STALE]") == 1


def test_name_lookup_is_accent_insensitive_and_lists_ambiguous_candidates(cli_db):
    assert "André Lacroix" in cli("show", "andre LACROIX").output
    res = cli("show", "Murphy")
    assert res.exit_code == 1
    assert "ambiguous" in res.output
    assert "p:nm:00445790:r10" in res.output and "p:nm:00445790:r11" in res.output
    assert "Dr Gerry Murphy" in cli("show", "Gerry Murphy").output
    assert cli("show", "Nobody Here").exit_code == 1


def test_merge_refuses_same_person_and_contradicting_cannot_link(cli_db):
    assert "already one person" in cli("merge", "nm:00445790:r9", "nm:04967001:r12",
                                       "-r", "x").output
    cli("split", "Chris Kennedy", "--move", "itv", "-r", "x", "-y")
    res = cli("merge", "nm:00445790:r9", "nm:04967001:r12", "-r", "x", "-y")
    assert res.exit_code == 1 and "Refused" in res.output


def test_undo_rejects_unknown_or_already_revoked(cli_db):
    res = cli("undo", "dec:9", "-y")
    assert res.exit_code == 1 and "Refused: no such decision" in res.output
    cli("set-fact", "seat:silver:moelis", "board_seat", "no", "-r", "x", "-y")
    cli("undo", "dec:1", "-y")
    assert "already revoked" in cli("undo", "dec:1", "-y").output


def test_review_lists_items_with_commands(cli_db):
    out = cli("review").output
    assert "1 review items" in out
    assert "-> pennant set-fact seat:silver:moelis board_seat no" in out


def test_run_prints_gate_report_and_diff(cli_db):
    res = cli("run", "--offline")
    assert res.exit_code == 0, res.output
    assert "run:2: staged" in res.output and "G1 pass" in res.output
    assert "Not published; votes still come from run:1." in res.output
    assert "Diff: no changes from run:1" in res.output


def test_run_exits_nonzero_when_held(cli_db, monkeypatch):
    import pennant.cli as cli_mod

    def held(settings, offline, publish, author):
        conn = db.connect(settings.db_path)
        run_id = db.create_run(conn, "h")
        db.finish_run(conn, run_id, "held", {"gates": {
            "G2": {"ok": False, "problems": ["tesco officers unavailable"]},
            "G4": {"ok": False, "problems": [f"nm:{i}: no recommendation" for i in range(50)]}}})
        return run_id

    monkeypatch.setattr(cli_mod, "run_pipeline", held)
    res = cli("run", "--publish")
    assert res.exit_code == 1
    assert "G2 FAIL" in res.output and "- tesco officers unavailable" in res.output
    assert "votes still come from run:1" in res.output
    assert "nm:9: no recommendation" in res.output and "nm:10:" not in res.output
    assert "… and 40 more" in res.output


def test_diff_defaults_and_bad_ids(cli_db):
    cli("set-fact", "seat:silver:moelis", "board_seat", "no", "-r", "x", "-y")
    out = cli("diff").output
    assert "run:1 → run:2" in out and "decision dec:1 applied" in out
    assert "run:2 → run:1" in cli("diff", "run:2", "run:1").output
    assert cli("diff", "run:9").exit_code == 1


def test_read_commands_need_a_run(tmp_path, monkeypatch):
    monkeypatch.setenv("PENNANT_DB", str(tmp_path / "empty.db"))
    res = cli("votes")
    assert res.exit_code == 1 and "pennant run" in res.output


def test_with_lane_g_decisions(cli_db, monkeypatch):
    """The same merge/undo and split flows through the real state/decisions.py, once it exists."""
    import pytest

    decisions = pytest.importorskip("pennant.state.decisions")
    import pennant.cli as cli_mod

    monkeypatch.setattr(cli_mod, "add_decision", decisions.add_decision)
    monkeypatch.setattr(cli_mod, "revoke", decisions.revoke)
    before = published(cli_db, "recommendation")
    assert cli("merge", "Ken Murphy", "Gerry Murphy", "-r", "x", "-y").exit_code == 0
    assert cli("undo", "dec:1", "-y").exit_code == 0
    assert published(cli_db, "recommendation") == before
    assert cli("split", "Chris Kennedy", "--move", "itv", "-r", "x", "-y").exit_code == 0
    res = cli("merge", "nm:00445790:r9", "nm:04967001:r12", "-r", "x", "-y")
    assert res.exit_code == 1 and "Refused" in res.output
    res = cli("set-fact", "seat:silver:moelis", "board_seat", "no", "-r", "adviser only", "-y")
    assert res.exit_code == 0 and "REVIEW -> FOR" in res.output
    assert "already revoked" in cli("undo", "dec:1", "-y").output


def test_end_to_end_with_lane_g_pipeline_gates_and_diff(lane_g_db):
    """Real run_pipeline, gates, decisions and diff report; only the stages are a fixture."""
    res = cli("run", "--offline", "--publish")
    assert res.exit_code == 0, res.output
    assert "run:1: published" in res.output and "G1 pass" in res.output
    assert len(table_rows(cli("votes", "--meeting", "tesco").output)) == 11
    before = published(lane_g_db, "recommendation")

    res = cli("split", "Chris Kennedy", "--move", "itv", "--reason", "different DOB", "-y")
    assert res.exit_code == 0, res.output
    assert "run:2: published" in res.output and "G4 pass" in res.output
    assert "dec:1: cannot_link" in res.output.split("Diff:")[1]  # the diff names it
    people = {p.person_id for p in published(lane_g_db, "person")}
    assert {"p:nm:00445790:r9", "p:nm:04967001:r12"} <= people

    res = cli("undo", "dec:1", "-y")
    assert res.exit_code == 0, res.output
    assert published(lane_g_db, "recommendation") == before

    res = cli("set-fact", "seat:silver:moelis", "board_seat", "no", "-r", "adviser only", "-y")
    assert res.exit_code == 0, res.output
    assert "dec:3" in res.output.split("Diff:")[1]
    silver = next(r for r in table_rows(cli("votes", "-m", "tesco").output) if "Silver" in r)
    assert "4–4" in silver and "FOR" in silver

    res = cli("set-fact", "nm:04967001:r13", "role_type_at_issuer", "chair", "-r", "AR p.51", "-y")
    assert res.exit_code == 0, res.output
    assert "run:5: published" in res.output and "G4 pass" in res.output


def test_set_fact_on_a_roster_entry_states_an_unstated_role(cli_db):
    row = next(r for r in table_rows(cli("votes", "-m", "itv").output) if "Lacroix" in r)
    assert "(not stated)" in row
    res = cli("set-fact", "p:nm:04967001:r13", "role_type_at_issuer", "chair", "-r", "AR p.51",
              "-y")
    assert res.exit_code == 0, res.output
    conn = db.connect(cli_db)
    (d,) = db.active_decisions(conn)
    assert d.payload == {"kind": "roster", "target": "nm:04967001:r13",
                         "field": "role_type_at_issuer", "value": "chair"}
    row = next(r for r in table_rows(cli("votes", "-m", "itv").output) if "Lacroix" in r)
    assert "Chair" in row
    res = cli("set-fact", "p:nm:00445790:r9", "role_type_at_issuer", "chair", "-r", "x", "-y")
    assert res.exit_code == 1 and "give one mention id" in res.output


# --- unpublished runs are marked on every output ---------------------------------


def _unpublished_run(path, status="held"):
    """A fixture run that was not published, finished with `status`."""
    import pennant.cli as cli_mod

    from .conftest import fake_run_pipeline

    return fake_run_pipeline(cli_mod.load_settings(), offline=True, publish=False,
                             author="system", status=status)


def test_nothing_published_marks_every_meeting_person_and_vote(tmp_path, monkeypatch):
    path = tmp_path / "unpublished.db"
    monkeypatch.setenv("PENNANT_DB", str(path))
    run_id = _unpublished_run(path)
    out = cli("votes").output
    headers = [ln for ln in out.splitlines() if " — AGM " in ln]
    assert len(headers) == 2
    assert all(f"[{run_id}, held, NOT PUBLISHED]" in h for h in headers)
    assert "nothing is published yet" in out
    show = cli("show", "Caroline Silver").output
    assert f"Caroline Silver  (p:nm:00445790:r13)  [{run_id}, held, NOT PUBLISHED]" in show
    assert f"Vote at Tesco: REVIEW  (NOT PUBLISHED: {run_id} is held)" in show
    assert f"[{run_id}, held, NOT PUBLISHED]" in cli("review").output


def test_an_explicit_unpublished_run_is_marked_and_the_published_one_is_not(cli_db):
    run_id = _unpublished_run(cli_db, status="staged")
    assert f"[{run_id}, staged, NOT PUBLISHED]" in cli("votes", "--run", run_id).output
    published_out = cli("votes").output
    assert "NOT PUBLISHED" not in published_out and "[run:1]" in published_out


def test_run_report_when_nothing_is_published(tmp_path, monkeypatch):
    import pennant.cli as cli_mod

    from .conftest import fake_run_pipeline

    monkeypatch.setenv("PENNANT_DB", str(tmp_path / "fresh.db"))
    monkeypatch.setattr(cli_mod, "run_pipeline", fake_run_pipeline)
    res = cli("run", "--offline")
    assert "run:1: staged" in res.output
    assert "no run is published yet" in res.output and "None" not in res.output


def test_every_role_type_has_a_label():
    from pennant.cli_view import ROLE_LABELS
    from pennant.core.models import RoleType

    assert set(RoleType) <= set(ROLE_LABELS)
    assert ROLE_LABELS["officer_title"] == "Officer (board unknown)"


def test_merge_takes_a_ch_officer_id_as_review_items_suggest(cli_db):
    """`unanchored` items suggest `merge <mention> <officer_id>`; the officer id goes into
    the must-link as given (CONTRACT_CHANGES D1)."""
    oid = "3foiCz6Hg_2s4JTbIRFd01JBwG4"
    res = cli("merge", "nm:00445790:r13", oid, "-r", "same person", "-y")
    assert res.exit_code == 0, res.output
    (d,) = [d for d in db.active_decisions(db.connect(cli_db)) if d.kind == "must_link"]
    assert d.payload == {"a": "nm:00445790:r13", "b": oid}
    already = cli("merge", "Chris Kennedy", "off-kennedy", "-r", "x", "-y")
    assert already.exit_code == 1 and "already one person" in already.output


def test_write_commands_show_the_change_and_record_nothing_unless_confirmed(cli_db):
    """Every write command previews what it will record, warns that it is replayed on every
    run, and asks first (default no). Declining leaves the decision log untouched."""
    def decisions():
        return len(db.all_decisions(db.connect(cli_db)))

    no = runner.invoke(app, ["set-fact", "seat:silver:moelis", "board_seat", "no", "-r", "x"],
                       input="n\n")
    assert no.exit_code == 1 and "Nothing recorded" in no.output
    assert "board_seat: unknown → no" in no.output and "affects Caroline Silver" in no.output
    assert "replays it until you `pennant undo` it" in no.output and decisions() == 0
    blank = runner.invoke(app, ["merge", "Chris Kennedy", "Caroline Silver", "-r", "x"], input="\n")
    assert blank.exit_code == 1 and decisions() == 0  # Enter alone means no
    yes = runner.invoke(app, ["set-fact", "seat:silver:moelis", "board_seat", "no", "-r", "x"],
                        input="y\n")
    assert yes.exit_code == 0 and "Recorded dec:1" in yes.output and decisions() == 1
    undo = runner.invoke(app, ["undo", "dec:1"], input="n\n")
    assert "Undo dec:1: set_fact" in undo.output and decisions() == 1


def test_history_follows_one_director_across_runs_with_causes(lane_g_db):
    """`pennant history`: each vote change with the decision behind it, the seat change it
    rests on, and every decision about the director (revokes included). Followed by notice
    mention, so a split and its undo don't lose the thread."""
    assert cli("run", "--offline", "--publish").exit_code == 0
    cli("set-fact", "seat:silver:moelis", "board_seat", "no", "-r", "adviser only", "-y")
    cli("undo", "dec:1", "-y")
    cli("split", "Chris Kennedy", "--move", "itv", "-r", "test", "-y")
    out = cli("history", "Caroline Silver").output
    lines = [ln for ln in out.splitlines() if ln.startswith("run:")]
    assert [ln.split()[0] for ln in lines] == ["run:1", "run:2", "run:3"]  # changes only
    assert "first run" in lines[0] and "FOR" in lines[1] and lines[1].endswith("dec:1")
    assert "REVIEW" in lines[2] and lines[2].endswith("dec:2")
    assert "board_seat unknown → no  (dec:1)" in out
    assert "set seat seat:silver:moelis board_seat = no" in out and "revoked by dec:2" in out
    assert "split nm:04967001:r12 from nm:00445790:r9" not in out  # not about her
    every = cli("history", "Caroline Silver", "--all").output
    assert len([ln for ln in every.splitlines() if ln.startswith("run:")]) == 4
    kennedy = cli("history", "nm:04967001:r12").output
    assert "split nm:04967001:r12 from nm:00445790:r9" in kennedy
