"""M5.2 accept tests on recorded web fixtures (tests/fixtures/web), fully offline."""

from __future__ import annotations

import json

import pytest

from pennant.classify import research
from pennant.classify.research import org_key, research_org, seat_listed
from pennant.classify.websearch import WEB_FIXTURES, load_fixtures, on_site, validate
from pennant.core import db
from pennant.core.models import RunContext


@pytest.fixture
def web(ctx: RunContext) -> RunContext:
    assert load_fixtures(ctx.conn) > 0
    return ctx


@pytest.mark.parametrize(("name", "as_of"), [
    ("Keywords Studios Ltd", "2026-06-18"),  # taken private by EQT, 2024
    ("Ocean Outdoor", "2026-06-18"),  # taken private, 2022
])
def test_taken_private_is_not_listed(web: RunContext, name: str, as_of: str) -> None:
    org = research_org(name, as_of, web)
    # equity no; with no source on debt, `listed` stays unknown, and for the policy it is no
    assert org.equity_listing == "no" and org.listed != "yes"
    assert seat_listed(org, web.policy) == "no"
    assert org.evidence and all(e.source == "web" and e.quote for e in org.evidence)


def test_capri_is_nyse_listed(web: RunContext) -> None:
    org = research_org("Capri Holdings Limited", "2026-05-20", web)
    assert org.listed == "yes" and "NYSE" in org.market.replace("New York Stock Exchange", "NYSE")


def test_ig_design_is_aim(web: RunContext) -> None:
    org = research_org("IG Design Group plc", "2026-06-18", web)
    assert org.equity_listing == "yes" and org.market.startswith("AIM")
    for flag in ("yes", "no", "unknown"):  # Gilliland follows the AIM flag (H13)
        policy = web.policy.model_copy(update={"aim_counts_as_listed": flag})
        assert seat_listed(org, policy) == flag


def test_ice_clear_europe_parent(web: RunContext) -> None:
    org = research_org("ICE Clear Europe", "2026-06-18", web)
    assert org_key(org.parent_name or "") == "intercontinental-exchange"


def test_every_fixture_hits_the_cache_offline(web: RunContext) -> None:
    for path in sorted(WEB_FIXTURES.glob("org__*.json")):
        inputs = json.loads(path.read_text())["payload"]["inputs"]
        research_org(inputs["org_key"], inputs["as_of"], web)
    misses = [r for r in db.get_outputs(web.conn, web.run_id, "review") if "offline" in r.detail]
    assert misses == []


def test_unreturned_url_in_a_real_answer_is_unknown() -> None:
    fx = json.loads((WEB_FIXTURES / "org__2026-05-20__capri-holdings.json").read_text())
    payload = fx["payload"]
    values, _, _ = validate(payload)
    assert values["equity_listed"] == "yes"
    text = next(b for b in reversed(payload["content"]) if b["type"] == "text")
    answer = json.loads(text["text"].split("```json")[-1].split("```")[0])
    answer["equity_listed"]["url"] = "https://not-in-the-search-results.example/capri"
    text["text"] = "```json\n" + json.dumps(answer) + "\n```"
    values, _, rejected = validate(payload)
    assert "equity_listed" not in values
    assert any("not-in-the-search-results" in r for r in rejected)


def test_stage_on_extract_fixtures_is_fully_recorded(web: RunContext) -> None:
    """Lane A's extract stage, then classify, offline: every question the stage asks has a
    recorded answer, and the H-case orgs come out as the catalog expects."""
    extract = pytest.importorskip("pennant.extract.validate")
    assert extract.run(web).ok
    report = research.run(web)
    misses = [r.subject for r in db.get_outputs(web.conn, web.run_id, "review")
              if "offline" in r.detail]
    assert report.ok and misses == []
    orgs = {o.org_key: o for o in db.get_outputs(web.conn, web.run_id, "org")}
    assert orgs["ig-design-group"].market.startswith("AIM")
    assert orgs["keywords-studios"].equity_listing == "no"  # debt listing unknown
    assert org_key(orgs["ice-clear-europe"].parent_name or "") == "intercontinental-exchange"
    seats = db.get_outputs(web.conn, web.run_id, "seat")
    fedex = [s for s in seats if s.org_key == "fedex" and s.current == "yes"]
    assert fedex and all(s.board_seat == "unknown" for s in fedex)  # an EVP title: rules can't say


def test_full_offline_chain_is_fully_recorded(web: RunContext) -> None:
    """extract → CH → dedup → classify on the committed fixtures: CH-only directorships
    (undisclosed seats) also have recorded answers, and H17's Lloyds banks join the group."""
    ch = pytest.importorskip("pennant.ch.client")
    cluster = pytest.importorskip("pennant.dedup.cluster")
    extract = pytest.importorskip("pennant.extract.validate")
    if not ch.load_fixtures(web.conn):
        pytest.skip("no CH fixtures")
    for stage in (extract, ch, cluster, research):
        assert stage.run(web).ok
    failed = [r.subject for r in db.get_outputs(web.conn, web.run_id, "review")
              if r.kind == "web_research_failed"]
    assert failed == []
    orgs = {o.org_key: o for o in db.get_outputs(web.conn, web.run_id, "org")}
    for bank in ("lloyds-bank", "bank-of-scotland"):
        assert org_key(orgs[bank].parent_name or "") == "lloyds-banking-group"
    # A role is only ever answered from the firm's own site. The search tool's site limit
    # still let SEC filings through for FedEx, Moelis and Pearson; validation drops them.
    sites = {k["inputs"]["domain"] for k in web_kinds(web) if k["kind"] == "position"}
    refs = [e.ref for p in db.get_outputs(web.conn, web.run_id, "position") for e in p.evidence]
    assert refs and all(any(on_site(u, d) for d in sites) for u in refs)
    assert not any("sec.gov" in u for u in refs)


def web_kinds(ctx: RunContext) -> list[dict]:
    rows = ctx.conn.execute("SELECT payload FROM cache WHERE namespace = 'web'").fetchall()
    return [json.loads(r[0]) for r in rows]
