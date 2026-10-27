"""M9.1: the run is exported as the gold scorer's CSVs and graded against eval/gold/."""

from __future__ import annotations

import csv
import importlib.util

import pytest

from eval.export import SEAT_COLUMNS, export_run
from eval.metrics import GOLD_DIR, ROOT, gold_flags, load_run, load_scorer, main
from pennant.core import db
from pennant.core.config import load_policy
from pennant.policy import score
from tests.policy.seat_cases import CASES, load_case, seed


@pytest.fixture
def scored(ctx, tmp_path):
    """A published run scored from every seat fixture (19 roster entries, 17 people)."""
    seed(ctx, [load_case(c) for c in CASES])
    assert score.run(ctx).ok
    db.finish_run(ctx.conn, ctx.run_id, "staged", {})
    db.publish(ctx.conn, ctx.run_id)
    return ctx, tmp_path


def rows(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def test_gold_scorer_selftest_passes():
    load_scorer(GOLD_DIR).selftest()  # catches an injected wrong AGAINST, split and past seat


def test_export_writes_the_three_csvs_the_scorer_reads(scored):
    ctx, tmp = scored
    r = load_run(ctx.conn, ctx.run_id)
    out = export_run(tmp / "out", r, load_policy())
    roster, votes, seats = (rows(out / f) for f in ("roster.csv", "votes.csv", "seats.csv"))
    assert len(roster) == len(votes) == 19
    assert {v["vote"] for v in votes} <= {"FOR", "AGAINST", "REVIEW"}
    kennedy = {x["person_id"] for x in roster if x["mention"] in
               {"nm:00445790:r9", "nm:04967001:r12"}}
    assert len(kennedy) == 1  # one person across Tesco and ITV
    assert list(seats[0]) == list(SEAT_COLUMNS)
    assert {s["counted"] for s in seats} <= {"yes", "no", "unknown"}
    # a past role is never counted (Hodgson's Standard Chartered)
    assert all(s["counted"] == "no" for s in seats if "Standard Chartered" in s["org"])


def test_perfect_votes_score_clean_and_one_wrong_against_is_caught(scored):
    """Grade the run's own export after setting every vote to gold's answer, then break one."""
    ctx, tmp = scored
    scorer = load_scorer(GOLD_DIR)
    gold = {e["mention_id"]: e for e in scorer.load_gold()["votes"]}
    out = export_run(tmp / "out", load_run(ctx.conn, ctx.run_id), load_policy())
    votes = rows(out / "votes.csv")
    flags = gold_flags(load_policy())
    for v in votes:  # a withdrawn resolution has no gold vote: anything but AGAINST is fine
        g = gold[v["mention"]]  # gold's answer under the repo's policy flags
        v.update(vote=scorer.gold_vote(g, flags) or "REVIEW", points_lo=g["points_lo"],
                 points_hi=g["points_hi"])

    def grade(vs):
        with (out / "votes.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(vs[0]))
            w.writeheader()
            w.writerows(vs)
        return scorer.score(out, gold_flags(load_policy()))

    m, _ = grade(votes)
    # every exported vote is right; gold's other entries are missing from the fixtures
    scored = [e for e in gold.values() if e["expected_vote"]]
    exported = {v["mention"] for v in votes}
    assert (m["wrong_against"], m["wrong_for"]) == (0, 0)
    assert m["vote_accuracy"] == pytest.approx(
        sum(e["mention_id"] in exported for e in scored) / len(scored))
    murphy = next(v for v in votes if v["mention"] == "nm:00445790:r10")  # Gerry Murphy, 4 pts
    murphy["vote"] = "AGAINST"
    m, lines = grade(votes)
    assert m["wrong_against"] == 1 and any("Gerry Murphy" in x for x in lines)


def test_report_grades_the_run_against_gold(scored):
    ctx, tmp = scored
    text = main(conn=ctx.conn, out=tmp / "report.md", csv_dir=tmp / "out")
    assert (tmp / "report.md").read_text() == text
    assert (tmp / "out" / "seats.csv").exists()
    assert "**Wrong AGAINST votes: " in text and "## Graded against the gold set" in text
    # the fixtures hold 19 of gold's 50 resolutions: the rest show as missing, not wrong
    assert "Gold covers 19 of 19 roster entries in this run" in text
    assert "recall 38.0% (19/50)" in text
    assert "## Gold-graded under each AIM value" in text
    assert "| yes |" in text and "| no |" in text
    rows_ = {line.split(" | ")[1]: line for line in text.splitlines() if line.startswith("| H")}
    assert "| FAIL |" not in rows_["Executive rule"]  # N/A: McCall (ITV) isn't in these fixtures
    assert "Thierry Garnier's resolution was withdrawn: never AGAINST" in rows_
    assert "| PASS |" in rows_["Stewart Gilliland follows the AIM flag"]


def test_meetings_outside_gold_are_reported_not_graded(scored):
    """The pipeline takes any notice: an entry gold doesn't label is shown as not covered."""
    ctx, tmp = scored
    m = next(x for x in db.get_outputs(ctx.conn, ctx.run_id, "meeting") if x.key == "tesco")
    entries = [e for e in db.get_outputs(ctx.conn, ctx.run_id, "roster")
               if e.meeting_id == m.meeting_id]
    acme = m.model_copy(update={"meeting_id": "mtg:09999999:2027-05-01", "key": "acme",
                                "company_name": "Acme Holdings plc",
                                "company_number": "09999999"})
    db.put_outputs(ctx.conn, ctx.run_id, "meeting", [acme])
    db.put_outputs(ctx.conn, ctx.run_id, "roster", [
        e.model_copy(update={"meeting_id": acme.meeting_id,
                             "mention_id": e.mention_id.replace("00445790", "09999999")})
        for e in entries[:1]])
    text = main(conn=ctx.conn, out=tmp / "report.md", csv_dir=tmp / "out")
    assert "Gold covers 19 of 20 roster entries in this run; not in gold: acme" in text
    assert "not on the gold roster: nm:09999999:" in text


def test_report_without_a_gold_set_still_describes_the_run(scored):
    ctx, tmp = scored
    (tmp / "gold").mkdir()
    (tmp / "gold" / "hard_cases.yaml").write_text("[]")
    text = main(conn=ctx.conn, gold_dir=tmp / "gold", out=tmp / "report.md")
    assert "No gold set" in text and "| all | 19 |" in text


def test_cli_loads_metrics_by_file_path(scored):
    """`pennant eval` loads eval/metrics.py by path, outside the `eval` package."""
    ctx, tmp = scored
    spec = importlib.util.spec_from_file_location("pennant_eval_metrics",
                                                  ROOT / "eval" / "metrics.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = module.main(conn=ctx.conn, out=tmp / "r.md", csv_dir=tmp / "out")
    assert "## Hard cases" in text and "## Graded against the gold set" in text
