"""M7.1: run lifecycle, the publish pointer, and one test per gate (stub stages)."""

import pytest

from pennant.core import db
from pennant.core.cache import put_cache
from pennant.core.models import Evidence, ReviewItem, notice_ref
from pennant.state import decisions as dec
from pennant.state import pipeline
from pennant.state.inputs import load_outputs, recs_by_mention
from pennant.state.pipeline import quote_grounded as real_quote_grounded

from .conftest import GERRY, ITV, KENNEDY_I, KENNEDY_T, MOELIS, NEBHRAJANI, SILVER, TESCO


def votes(conn, run_id):
    return {m: r.vote for m, r in recs_by_mention(load_outputs(conn, run_id)).items()}


def test_clean_run_stages_then_publishes(conn, run):
    staged = run()
    assert staged.status == "staged" and all(g.ok for g in staged.gates.values())
    assert db.published_run_id(conn) is None
    published = run(publish=True)
    assert published.status == "published"
    assert db.published_run_id(conn) == published.run_id
    assert published.previous_published is None
    report = db.get_run(conn, published.run_id)["gate_report"]
    assert set(report["gates"]) == {"G1", "G2", "G3", "G4"}
    assert report["inputs"]["notices"]  # notice hashes recorded for G3 and diff
    assert all(g["ok"] for g in report["gates"].values())


def test_stage_exception_fails_and_keeps_pointer(conn, run, world):
    first = run(publish=True)
    world.boom = "dedup"
    result = run(publish=True)
    assert result.status == "failed"
    assert "stage dedup: RuntimeError" in result.error
    assert [r.stage for r in result.stages] == ["extract", "ch"]
    assert db.get_run(conn, result.run_id)["status"] == "failed"
    assert db.published_run_id(conn) == first.run_id


def test_gate_exception_fails_and_keeps_pointer(conn, run, world, monkeypatch):
    first = run(publish=True)

    def broken(ev):
        raise RuntimeError("pymupdf cannot open the PDF")

    monkeypatch.setattr(pipeline, "quote_grounded", broken)
    world.votes[GERRY] = "AGAINST"  # G1 now has a quote to ground
    result = run(publish=True)
    assert result.status == "failed" and result.gates == {}
    assert result.error == "gates: RuntimeError: pymupdf cannot open the PDF"
    row = db.get_run(conn, result.run_id)
    assert row["status"] == "failed"
    assert row["gate_report"]["error"] == result.error
    assert db.published_run_id(conn) == first.run_id


def test_missing_stage_module_fails_closed(conn):
    from pennant.core.config import Settings, load_policy

    result = pipeline.run_pipeline(Settings(db_path=":memory:"), offline=True, publish=True,
                                   conn=conn, policy=load_policy(),
                                   stages=[("extract", "pennant.no_such_stage")])
    assert result.status == "failed" and "ModuleNotFoundError" in result.error
    assert db.published_run_id(conn) is None


# --- G1 extraction -------------------------------------------------------------


def test_g1_non_contiguous_resolutions_hold(conn, run, world):
    world.gaps[TESCO] = (4, 6)  # resolution 5 missing
    result = run(publish=True)
    assert result.status == "held"
    assert not result.gates["G1"].ok
    assert "not contiguous: [4, 6]" in result.gates["G1"].problems[0]
    assert db.published_run_id(conn) is None


def test_g1_ungrounded_quote_behind_against_holds(conn, run, world):
    world.votes[GERRY] = "AGAINST"
    world.fabricate = GERRY
    result = run(publish=True)
    assert result.status == "held"
    assert any("ungrounded quote" in p for p in result.gates["G1"].problems)


def test_g1_ungrounded_quote_behind_for_is_not_g1s_business(run, world):
    world.fabricate = GERRY  # Gerry is FOR: the quote can't cause a wrong AGAINST
    assert run(publish=True).status == "published"


def test_g1_real_grounding_on_tesco_page_10():
    pytest.importorskip("pennant.evidence.grounding")
    ref = notice_ref("Tesco_2026_Notice_of_AGM.pdf", 10)
    real = Evidence(source="notice", ref=ref,
                    quote="Chair: Barratt Redrow plc and ICE Clear Europe")
    fake = Evidence(source="notice", ref=ref,
                    quote="Non-executive Director of Standard Chartered PLC since 2019")
    missing = Evidence(source="notice", ref=notice_ref("Nope.pdf", 1), quote="anything")
    assert real_quote_grounded(real)
    assert not real_quote_grounded(fake)
    assert not real_quote_grounded(missing)  # fails closed


def test_g1_uncovered_roster_entry_checks_its_seat_and_roster_quotes():
    """A roster entry no person covers is scored alone as p:<mention>; the quotes behind
    its AGAINST are its own roster entry's and seats', not just the recommendation's."""
    from pennant.core.models import RosterEntry, Seat

    ref = notice_ref("ITV_2026_Notice_of_AGM.pdf", 10)
    fake = Evidence(source="notice", ref=ref, quote="FABRICATED NED of Standard Chartered")
    roster_ev = Evidence(source="notice", ref=ref, quote="FABRICATED roster quote")
    mid = f"mtg:{ITV}:2026-05-07"
    out = {
        "meeting": [], "person": [], "org": [], "review": [], "officer": [],
        "roster": [RosterEntry(mention_id=NEBHRAJANI, meeting_id=mid, resolution_no=5,
                               name="Sharmila Nebhrajani", coverage="structured",
                               evidence=[roster_ev])],
        "seat": [Seat(seat_id=f"seat:{NEBHRAJANI}:sc", mention_id=NEBHRAJANI,
                      org_name="Standard Chartered", org_key="standard-chartered",
                      evidence=[fake])],
        "recommendation": [pipeline.Recommendation(
            person_id=f"p:{NEBHRAJANI}", meeting_id=mid, points_lo=6, points_hi=6,
            points_rule="yes", exec_rule="no", vote="AGAINST", coverage="structured")],
    }
    behind = pipeline.evidence_behind(out["recommendation"][0], out)
    assert {e.quote for e in behind} == {fake.quote, roster_ev.quote}
    problems = pipeline._extraction_problems(out)
    assert sum("ungrounded quote" in p for p in problems) == 2


# --- G2 Companies House (H16) --------------------------------------------------


def test_h16_ch_unavailable_for_one_issuer_holds_and_published_votes_stand(conn, run, world):
    first = run(publish=True)
    before = votes(conn, first.run_id)
    world.ch_down = {ITV}
    world.votes[NEBHRAJANI] = "REVIEW"  # what a CH-less run would compute
    result = run(publish=True)
    assert result.status == "held"
    g2 = result.gates["G2"]
    assert not g2.ok
    assert any("stage ch not ok" in p for p in g2.problems)
    assert any("no Companies House officers" in p and ITV in p for p in g2.problems)
    assert db.published_run_id(conn) == first.run_id
    assert votes(conn, db.published_run_id(conn)) == before


def test_g2_officers_missing_even_without_a_signal(run, world):
    """A CH stage that drops an issuer silently still fails G2: no officers on record."""
    world.ch_down, world.ch_silent = {TESCO}, True
    result = run(publish=True)
    assert result.status == "held"
    assert result.gates["G2"].problems == [
        f"mtg:{TESCO}:2026-06-18: no Companies House officers on record for Tesco PLC ({TESCO})"]


@pytest.mark.parametrize("stage", ["ch", "dedup"])
def test_h16_officer_level_outage_holds_and_published_votes_stand(conn, run, world, stage):
    """One director's appointments (CH stage) or officer search (dedup) unavailable hides
    her undisclosed seats, so her REVIEW can turn FOR. The run must not publish."""
    first = run(publish=True)
    before = votes(conn, first.run_id)
    assert before[SILVER] == "REVIEW"
    real = getattr(world, stage)

    def outage(ctx):
        rep = real(ctx)
        db.put_outputs(ctx.conn, ctx.run_id, "review", [ReviewItem(
            kind=pipeline.CH_UNAVAILABLE, subject=f"off-{SILVER}",
            detail="CH appointments unavailable; their other seats are unknown")])
        return rep  # ok=True: the stage itself saw only a fail-closed unknown

    setattr(world, stage, outage)
    world.votes[SILVER] = "FOR"  # her undisclosed seats vanished
    result = run(publish=True)
    assert result.status == "held"
    assert not result.gates["G2"].ok
    assert result.gates["G2"].problems == [
        f"off-{SILVER}: ch_unavailable: CH appointments unavailable; "
        "their other seats are unknown"]
    assert result.gates["G3"].ok  # REVIEW → FOR isn't G3's business; G2 holds it
    assert db.published_run_id(conn) == first.run_id
    assert votes(conn, db.published_run_id(conn)) == before


def test_h16_any_unavailable_kind_holds_a_first_run(run, world):
    real = world.dedup

    def dedup(ctx):
        rep = real(ctx)
        db.put_outputs(ctx.conn, ctx.run_id, "review", [ReviewItem(
            kind="appointments_unavailable", subject="off-y", detail="search failed")])
        return rep

    world.dedup = dedup
    result = run(publish=True)  # nothing published yet, so G3 couldn't catch it either
    assert result.status == "held" and not result.gates["G2"].ok


# --- G3 churn --------------------------------------------------------------------


def test_g3_flip_without_changed_input_holds(conn, run, world):
    first = run(publish=True)
    world.votes[GERRY] = "AGAINST"  # a wobble: nothing about the inputs changed
    result = run(publish=True)
    assert result.status == "held"
    assert not result.gates["G3"].ok
    assert f"{GERRY}: FOR → AGAINST" in result.gates["G3"].problems[0]
    assert db.published_run_id(conn) == first.run_id


CH = "https://api.company-information.service.gov.uk"


@pytest.mark.parametrize("namespace, key, payload", [
    ("ch", f"{CH}/officers/off-{GERRY}/appointments?items_per_page=50&start_index=0", {"a": 1}),
    ("ch", f"{CH}/company/{TESCO}/officers?items_per_page=100&start_index=0", {"a": 1}),
    ("ch", f"{CH}/search/officers?q=GERRY%20Murphy&items_per_page=100&start_index=0", {"a": 1}),
    ("web", "k1", {"kind": "org", "inputs": {"org_key": TESCO, "as_of": "2026-06-18"}}),
])
def test_g3_flip_with_changed_cache_entry_for_that_person_passes(
        conn, run, world, namespace, key, payload):
    run(publish=True)
    put_cache(conn, namespace, key, payload)
    world.votes[GERRY] = "AGAINST"
    assert run(publish=True).gates["G3"].ok


def test_g3_flip_with_unrelated_cache_changes_holds(conn, run, world):
    """A nightly run always refreshes some web entries; that mustn't excuse every flip."""
    run(publish=True)
    put_cache(conn, "web", "k2", {"kind": "org", "inputs": {"org_key": "moelis"}})
    put_cache(conn, "ch", f"{CH}/officers/off-{SILVER}/appointments", {"a": 1})
    put_cache(conn, "ch", f"{CH}/company/{ITV}/officers", {"a": 1})
    world.votes[GERRY] = "AGAINST"
    result = run(publish=True)
    assert result.status == "held"
    assert result.gates["G3"].problems == [
        f"{GERRY}: FOR → AGAINST with no changed input for this person since run:1"]


def test_g3_flip_with_policy_change_passes(conn, run, world):
    from pennant.core.config import load_policy

    run(publish=True, policy=load_policy().model_copy(update={"aim_counts_as_listed": "unknown"}))
    world.votes[GERRY] = "AGAINST"
    changed = load_policy().model_copy(update={"aim_counts_as_listed": "yes"})
    assert run(publish=True, policy=changed).gates["G3"].ok  # a policy change touches everyone


def test_g3_flip_with_new_decision_passes(conn, run, world):
    run(publish=True)
    dec.add_decision(conn, "must_link", {"a": KENNEDY_T, "b": KENNEDY_I}, "expert", "same DOB")
    result = run(publish=True)  # Kennedy FOR → AGAINST, explained by dec:1
    assert result.status == "published" and result.gates["G3"].ok


def test_g3_decision_about_someone_else_does_not_excuse_a_flip(conn, run, world):
    run(publish=True)
    dec.add_decision(conn, "set_fact", {"kind": "seat", "target": MOELIS, "field": "board_seat",
                                        "value": "no"}, "expert", "advisory")
    world.votes[GERRY] = "AGAINST"
    result = run(publish=True)
    assert result.status == "held"
    assert result.gates["G3"].problems[0].startswith(f"{GERRY}: FOR → AGAINST")


def test_g3_skipped_for_analyst_runs(run, world):
    run(publish=True)
    world.votes[GERRY] = "AGAINST"
    result = run(publish=True, author="expert")
    assert result.gates["G3"].skipped and result.status == "published"


def test_g3_review_is_not_a_flip(run, world):
    run(publish=True)
    world.votes[GERRY] = "REVIEW"
    assert run(publish=True).gates["G3"].ok


# --- G4 completeness -------------------------------------------------------------


def test_g4_roster_entry_without_recommendation_holds(run, world):
    world.skip_rec = {NEBHRAJANI}
    result = run(publish=True)
    assert result.status == "held"
    assert result.gates["G4"].problems == [f"{NEBHRAJANI}: no recommendation"]


def test_g4_unhonoured_set_fact_holds(conn, run, world):
    d = dec.add_decision(conn, "set_fact", {"kind": "seat", "target": MOELIS,
                                            "field": "board_seat", "value": "no"},
                         "expert", "Moelis role is advisory")
    world.ignore_decisions = True
    result = run(publish=True)
    assert result.status == "held"
    assert result.gates["G4"].problems[0].startswith(f"{d.decision_id}: {MOELIS}.board_seat")


def test_g4_honoured_set_fact_publishes_and_moves_the_vote(conn, run):
    run(publish=True)
    assert votes(conn, db.published_run_id(conn))[SILVER] == "REVIEW"
    dec.add_decision(conn, "set_fact", {"kind": "seat", "target": MOELIS,
                                        "field": "board_seat", "value": "no"}, "expert", "advisory")
    assert run(publish=True, author="expert").status == "published"
    assert votes(conn, db.published_run_id(conn))[SILVER] == "FOR"


def test_g4_superseded_set_fact_does_not_hold(conn, run, world):
    """The analyst corrected herself: the newer set-fact is applied, the older one is
    no longer expected, and the run publishes without undoing it."""
    old = dec.add_decision(conn, "set_fact", {"kind": "seat", "target": MOELIS,
                                              "field": "board_seat", "value": "yes"}, "expert", "a")
    dec.add_decision(conn, "set_fact", {"kind": "seat", "target": MOELIS,
                                        "field": "board_seat", "value": "no"}, "expert", "advisory")
    result = run(publish=True, author="expert")
    assert result.gates["G4"].ok, result.gates["G4"].problems
    assert result.status == "published"
    out = load_outputs(conn, result.run_id)
    assert next(s for s in out["seat"] if s.seat_id == MOELIS).board_seat == "no"
    assert votes(conn, result.run_id)[SILVER] == "FOR"
    dec.revoke(conn, old.decision_id, "expert", "tidy up")  # undoing it later changes nothing
    assert votes(conn, run(publish=True, author="expert").run_id)[SILVER] == "FOR"


def test_g4_unhonoured_must_link_holds(conn, run, world):
    d = dec.add_decision(conn, "must_link", {"a": GERRY, "b": NEBHRAJANI}, "expert", "test")
    world.ignore_links = True
    result = run(publish=True)
    assert result.status == "held"
    assert result.gates["G4"].problems == [f"{d.decision_id}: {GERRY} and {NEBHRAJANI} are "
                                           "not one person"]


# --- offline seeding ---------------------------------------------------------------


def _rows(conn, namespace=None):
    sql = "SELECT COUNT(*) FROM cache" + (" WHERE namespace = ?" if namespace else "")
    return conn.execute(sql, (namespace,) if namespace else ()).fetchone()[0]


def test_offline_seeding_loads_fixtures_once_and_never_shadows_live_rows(conn, monkeypatch):
    from pennant.core.cache import get_cache

    from .conftest import REAL_SEEDS

    monkeypatch.setattr(pipeline, "OFFLINE_SEEDS", REAL_SEEDS)
    issuer = f"{CH}/company/{TESCO}/officers?items_per_page=100&start_index=0"
    put_cache(conn, "ch", issuer, {"status": 200, "body": {"live": True}})  # newer, live
    added = pipeline.seed_offline_cache(conn)
    assert added > 0 and _rows(conn, "ch") > 1 and _rows(conn, "web") > 0
    assert get_cache(conn, "ch", issuer)[0] == {"status": 200, "body": {"live": True}}
    assert conn.execute("SELECT COUNT(*) FROM cache WHERE key = ?", (issuer,)).fetchone()[0] == 1
    total = _rows(conn)
    assert pipeline.seed_offline_cache(conn) == 0 and _rows(conn) == total  # no duplicates


def test_offline_run_seeds_the_cache_and_a_live_run_does_not(conn, run, monkeypatch):
    from pennant.core.config import Settings, load_policy

    from .conftest import REAL_SEEDS

    monkeypatch.setattr(pipeline, "OFFLINE_SEEDS", REAL_SEEDS)
    noop = [("extract", lambda ctx: pipeline.StageReport(stage="extract", ok=True))]
    live = pipeline.run_pipeline(Settings(db_path=":memory:"), offline=False, conn=conn,
                                 policy=load_policy(), stages=noop)
    assert live.error is None and _rows(conn) == 0
    first = run(publish=True)
    assert first.status == "published" and _rows(conn, "ch") and _rows(conn, "web")
    report = db.get_run(conn, first.run_id)["gate_report"]
    assert report["inputs"]["cache_max_id"] == _rows(conn)  # seeded before inputs were read
    total = _rows(conn)
    world_run = run(publish=True)
    assert _rows(conn) == total and world_run.gates["G3"].ok
