"""M5.2 offline: validation, failure paths, policy flags and the stage, on synthetic
responses (a fake `websearch.call`). Recorded-fixture oracles are in test_research_fixtures."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from pennant.classify import backends, research, websearch
from pennant.classify.research import org_key, research_org, seat_listed
from pennant.core import db
from pennant.core.config import Policy, Settings
from pennant.core.models import (
    CHAppointment,
    CHOfficer,
    Meeting,
    OrgFacts,
    Person,
    RosterEntry,
    RunContext,
    Seat,
)

URL = "https://www.example-exchange.com/listing"


def fact(value: Any, url: str | None = URL, quote: str | None = "a verbatim quote") -> dict:
    return {"value": value, "url": url, "quote": quote}


def payload(answer: dict[str, Any], urls: tuple[str, ...] = (URL,),
            errors: tuple[str, ...] = ()) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "server_tool_use", "query": "q"}]
    content.append({"type": "web_search_tool_result",
                    "results": [{"url": u, "title": "A page", "page_age": None} for u in urls]})
    content += [{"type": "web_search_tool_result", "error_code": e} for e in errors]
    content.append({"type": "text", "text": "Done.\n```json\n" + json.dumps(answer) + "\n```"})
    return {"model": "m", "stop_reason": "end_turn", "usage": {}, "content": content,
            "fetched_at": "2026-10-26T12:00:00+00:00"}


@pytest.fixture
def online(ctx: RunContext, monkeypatch: pytest.MonkeyPatch):
    """ctx with offline=False and a fake websearch.call; returns (ctx, set_answer, calls)."""
    calls: list[dict[str, Any]] = []
    answers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def fake_call(settings: Any, prompt: str, meta: dict[str, Any],
                  domains: list[str] | None = None) -> dict[str, Any]:
        calls.append({**meta, "domains": domains} if domains else meta)
        return {**answers[meta["kind"]](meta["inputs"]), **meta}

    monkeypatch.setattr(websearch, "call", fake_call)
    return dataclasses.replace(ctx, offline=False), answers, calls


def reviews(ctx: RunContext) -> list[Any]:
    return db.get_outputs(ctx.conn, ctx.run_id, "review")


def web_rows(ctx: RunContext) -> int:
    return ctx.conn.execute("SELECT COUNT(*) FROM cache WHERE namespace = 'web'").fetchone()[0]


def test_org_key() -> None:
    assert org_key("Barratt Redrow plc") == "barratt-redrow"
    assert org_key("The Magnum Ice Cream Company N.V.") == "magnum-ice-cream-company"
    assert org_key("Greenhill & Co. International LLP") == "greenhill-and-co-international"
    assert org_key("INTERCONTINENTAL EXCHANGE, INC.") == org_key("Intercontinental Exchange")
    assert org_key("Burberry Group plc") != org_key("The Burberry Foundation")


def test_parse_answer_takes_last_json_object() -> None:
    p = payload({"a": 1})
    p["content"].insert(0, {"type": "text", "text": '```json\n{"draft": true}\n```'})
    assert websearch.parse_answer(p) == {"a": 1}
    assert websearch.parse_answer({"content": [{"type": "text", "text": "no json"}]}) is None


def test_sourced_answer_maps_to_org_facts(online) -> None:
    ctx, answers, calls = online
    answers["org"] = lambda _: payload({
        "legal_name": fact("Capri Holdings Limited"), "equity_listed": fact("yes"),
        "exchange": fact("NYSE"), "ticker": fact("CPRI"), "market_segment": fact("main"),
        "debt_listed": fact("unknown", None, None), "is_subsidiary": fact("no"),
        "events": [{"date": "2025-04-14", "event": "sold Versace", "url": URL, "quote": "q"}]})
    org = research_org("Capri Holdings Limited", "2026-05-20", ctx)
    assert (org.listed, org.equity_listing, org.market) == ("yes", "yes", "NYSE (CPRI)")
    assert org.events == ["2025-04-14: sold Versace"]
    assert {e.ref for e in org.evidence} == {URL} and org.evidence[0].source == "web"
    research_org("Capri Holdings Ltd", "2026-05-20", ctx)  # same org_key: served from cache
    assert len(calls) == 1 and reviews(ctx) == []


def test_url_not_returned_by_search_makes_fact_unknown(online) -> None:
    ctx, answers, _ = online
    answers["org"] = lambda _: payload({"equity_listed": fact("yes", "https://made-up.example/x"),
                                        "is_subsidiary": fact("no", URL, None)})
    org = research_org("Somewhere plc", "2026-06-18", ctx)
    assert org.listed == "unknown" and org.evidence == []
    (item,) = reviews(ctx)
    assert item.kind == "web_unsourced" and "made-up.example" in item.detail
    assert "is_subsidiary has no quote" in item.detail


def test_search_error_is_unknown_with_review_and_not_cached(online) -> None:
    ctx, answers, calls = online
    answers["org"] = lambda _: payload({"equity_listed": fact("yes")},
                                       errors=("too_many_requests",))
    org = research_org("Capri Holdings Limited", "2026-05-20", ctx)
    assert org.listed == "unknown"
    (item,) = reviews(ctx)
    assert item.kind == "web_research_failed" and "too_many_requests" in item.detail
    assert web_rows(ctx) == 0
    research_org("Capri Holdings Limited", "2026-05-20", ctx)
    assert len(calls) == 2  # retried, because the failure was not cached


def test_max_uses_exceeded_is_not_an_error(online) -> None:
    ctx, answers, _ = online
    answers["org"] = lambda _: payload({"equity_listed": fact("no")}, errors=("max_uses_exceeded",))
    org = research_org("Ocean Outdoor", "2026-06-18", ctx)
    assert (org.equity_listing, seat_listed(org, Policy())) == ("no", "no")
    assert reviews(ctx) == [] and web_rows(ctx) == 1


def test_outage_is_unknown_and_auth_error_raises(online, monkeypatch) -> None:
    ctx, _, _ = online

    def outage(*_: Any) -> dict[str, Any]:
        raise backends.ModelUnavailable("APIConnectionError")

    monkeypatch.setattr(websearch, "call", outage)
    assert research_org("Capri Holdings Limited", "2026-05-20", ctx).listed == "unknown"
    assert reviews(ctx)[0].kind == "web_research_failed"

    def rejected_key(*_: Any) -> dict[str, Any]:
        response = httpx2.Response(401, request=REQUEST)
        raise anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)

    monkeypatch.setattr(websearch, "call", rejected_key)
    with pytest.raises(anthropic.AuthenticationError):  # stop and ask expert
        research_org("Man Group plc", "2026-07-09", ctx)


REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize("error, unavailable", [
    (anthropic.APIConnectionError(request=REQUEST), True),
    (anthropic.InternalServerError("down", response=httpx2.Response(503, request=REQUEST),
                                   body=None), True),
    (anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=REQUEST),
                                   body=None), False),
])
def test_anthropic_provider_turns_transient_errors_into_model_unavailable(
        monkeypatch, error, unavailable) -> None:
    class Client:
        def __init__(self, **_: Any) -> None:
            self.beta = self
            self.messages = self

        def create(self, **_: Any) -> None:
            raise error

    monkeypatch.setattr(backends.anthropic, "Anthropic", Client)
    expected = backends.ModelUnavailable if unavailable else type(error)
    with pytest.raises(expected):
        backends.anthropic_search(Settings(anthropic_api_key="k"), "m", "prompt")


def test_llm_model_names_the_provider_and_unknown_ones_are_refused() -> None:
    assert backends.provider("claude-sonnet-5-5") == (backends.anthropic_search,
                                                       "claude-sonnet-5-5")
    assert backends.provider("anthropic:claude-x")[1] == "claude-x"
    with pytest.raises(ValueError, match="no research provider 'acme'"):
        backends.provider("acme:model-1")


def test_offline_miss_is_unknown_with_review(ctx: RunContext) -> None:
    org = research_org("Capri Holdings Limited", "2026-05-20", ctx)
    assert org.listed == "unknown"
    assert "offline" in reviews(ctx)[0].detail


def test_debt_only_and_aim_follow_policy_flags(online) -> None:
    ctx, answers, _ = online
    answers["org"] = lambda inputs: payload({
        "equity_listed": fact("no" if inputs["org_key"] == "lloyds-bank" else "yes"),
        "debt_listed": fact("yes"),
        "market_segment": fact("aim"), "exchange": fact("London Stock Exchange")})
    bank = research_org("Lloyds Bank plc", "2026-07-09", ctx)
    aim = research_org("IG Design Group plc", "2026-06-18", ctx)
    assert (bank.listed, bank.equity_listing) == ("yes", "no")
    assert aim.market.startswith("AIM")
    default = ctx.policy.model_copy(update={"debt_only_listing_counts": "no",
                                             "aim_counts_as_listed": "unknown"})
    assert seat_listed(bank, default) == "no"
    debt_counts = default.model_copy(update={"debt_only_listing_counts": "yes"})
    assert seat_listed(bank, debt_counts) == "yes"
    assert seat_listed(aim, default) == "unknown"  # aim_counts_as_listed: unknown
    for flag in ("yes", "no"):
        assert seat_listed(aim, default.model_copy(update={"aim_counts_as_listed": flag})) == flag


def test_seat_listed_rule_and_missing_org() -> None:
    policy = Policy()
    assert seat_listed(None, policy) == "unknown"
    ruled = OrgFacts(org_key="k", canonical_name="K", listed="no", equity_listing="no")
    assert seat_listed(ruled, policy) == "no"


def test_debt_counts_only_when_found_and_never_for_non_companies() -> None:
    """With debt-only listings counting, a company whose shares aren't listed counts only
    if a bond listing was found; charities, public bodies, education bodies and
    partnerships never count, whatever research left open."""
    debt = Policy(debt_only_listing_counts="yes")
    found = OrgFacts(org_key="b", canonical_name="B", org_type="company",
                     listed="yes", equity_listing="no")
    not_found = OrgFacts(org_key="p", canonical_name="P", org_type="company",
                         listed="unknown", equity_listing="no")
    open_equity = not_found.model_copy(update={"equity_listing": "unknown"})
    assert seat_listed(found, debt) == "yes"
    assert seat_listed(not_found, debt) == "no"
    assert seat_listed(open_equity, debt) == "unknown"  # shares not settled: still open
    # bonds found, shares unknown: the bonds settle it when they count, not otherwise
    bonds_open_equity = found.model_copy(update={"equity_listing": "unknown"})
    assert seat_listed(bonds_open_equity, debt) == "yes"
    assert seat_listed(bonds_open_equity, Policy(debt_only_listing_counts="no")) == "unknown"
    for kind in ("charity", "public_body", "Public body", "education", "partnership"):
        body = open_equity.model_copy(update={"org_type": kind})
        assert seat_listed(body, debt) == "no", kind


# --- the stage -------------------------------------------------------------------

MTG = "mtg:04267576:2026-05-20"


def seat(n: int, org: str, role: str, current: str = "yes", **kw: Any) -> Seat:
    return Seat(seat_id=f"s{n}", mention_id=kw.pop("mention", "nm:04267576:r10"), org_name=org,
                org_key=org_key(org), role_text=role, current=current, **kw)


def test_stage_rules_then_research_only_where_it_matters(online) -> None:
    """Rules settle roles; the open web is asked about organisations. A role the rules leave
    open (an EVP or CFO title: board seat unknown) is asked of the firm's own site only."""
    ctx, answers, calls = online
    org_answers = {
        "intertek-group": {"equity_listed": fact("yes"), "exchange": fact("LSE"),
                           "market_segment": fact("main"), "country": fact("United Kingdom")},
        "fedex": {"equity_listed": fact("yes"), "exchange": fact("NYSE"),
                  "market_segment": fact("main"),
                  "country": fact("United States")},
        "haleon": {"equity_listed": fact("yes"), "country": fact("England and Wales")}}
    answers["org"] = lambda inputs: payload(org_answers[inputs["org_key"]])
    sites = {"fedex": "https://www.fedex.com", "haleon": "https://www.haleon.com"}

    def site(inputs):
        page = sites[inputs["org_key"]] + "/about"
        return payload({"website": fact(sites[inputs["org_key"]], page,
                                        f"About {inputs['org_key'].title()}")}, urls=(page,))

    def position(inputs):
        page = sites[inputs["org_key"]] + "/board"
        board = fact("no", page, "Board of Directors") if inputs["org_key"] == "fedex" \
            else fact("executive_director", page, "Dawn Allen, Chief Financial Officer")
        key = "board_member" if inputs["org_key"] == "fedex" else "capacity"
        return payload({key: board}, urls=(page,))

    answers["site"], answers["position"] = site, position
    put = lambda kind, models: db.put_outputs(ctx.conn, ctx.run_id, kind, models)  # noqa: E731
    put("meeting", [Meeting(meeting_id=MTG, company_name="Intertek Group plc",
                            company_number="04267576", agm_date="2026-05-20", doc_sha="x")])
    put("roster", [RosterEntry(mention_id="nm:04267576:r10", meeting_id=MTG, resolution_no=10,
                               name="Kawal Preet", coverage="structured")])
    put("person", [Person(person_id="p:nm:04267576:r10", mention_ids=["nm:04267576:r10"],
                          officer_ids=["off1"])])
    put("officer", [CHOfficer(officer_id="off1", name="PREET, Kawal", appointments=[
        CHAppointment(company_number="09999999", company_name="KEYWORDS STUDIOS LIMITED",
                      company_type="ltd", role="director"),
        CHAppointment(company_number="01111111", company_name="OLD CO PLC", company_type="plc",
                      role="director", resigned_on="2020-01-01")])])
    put("seat", [
        seat(1, "Intertek Group plc", "Non-Executive Director"),
        seat(2, "FedEx", "Executive Vice President, Planning, Engineering and Transformation"),
        seat(3, "Haleon PLC", "Chief Financial Officer"),  # board seat: CH decides
        seat(4, "Asia Pacific Ltd", "Non-executive Director", current="no"),  # past: no calls
        seat(5, "Keywords Studios Ltd", "Director"),
        seat(6, "Almeida Theatre", "Trustee"),  # current, but never a board seat: no call
    ])
    report = research.run(ctx)
    assert report.ok and report.stage == "classify"
    assert report.counts["org_research"] == 3  # intertek, fedex, haleon
    assert report.counts["listed_by_rule"] == 1  # keywords: ltd on CH
    seats = {s.seat_id: s for s in db.get_outputs(ctx.conn, ctx.run_id, "seat")}
    orgs = {o.org_key: o for o in db.get_outputs(ctx.conn, ctx.run_id, "org")}
    assert (seats["s1"].role_type, seats["s1"].board_seat, seats["s1"].listed) == (
        "non_executive_director", "yes", "yes")
    fedex = seats["s2"]
    assert (fedex.role_type, fedex.board_seat, fedex.executive, fedex.listed) == (
        "officer_title", "unknown", "yes", "yes")
    assert seats["s3"].board_seat == "unknown"
    assert orgs["asia-pacific"].listed == "unknown" and seats["s4"].current == "no"
    assert orgs["keywords-studios"].listed == "no" and seats["s5"].listed == "no"
    assert orgs["keywords-studios"].evidence[0].source == "ch"
    assert "old-co" not in orgs  # resigned before the AGM
    assert orgs["almeida-theatre"].listed == "unknown" and seats["s6"].board_seat == "no"
    assert [c["kind"] for c in calls] == ["org"] * 3 + ["site", "position"] * 2
    assert [c.get("domains") for c in calls if c["kind"] == "position"] == [
        ["fedex.com"], ["haleon.com"]]  # role questions search the firm's own site only
    pos = {p.seat_id: p for p in db.get_outputs(ctx.conn, ctx.run_id, "position")}
    assert pos["s2"].board_seat == "no"
    assert (pos["s3"].board_seat, pos["s3"].executive, pos["s3"].role_type) == (
        "yes", "yes", "executive_director")


def test_stage_marks_issuer_seat_as_board_seat(online) -> None:
    ctx, answers, _ = online
    answers["org"] = lambda _: payload({"equity_listed": fact("yes")})
    db.put_outputs(ctx.conn, ctx.run_id, "meeting", [Meeting(
        meeting_id=MTG, company_name="Intertek Group plc", company_number="04267576",
        agm_date="2026-05-20", doc_sha="x")])
    db.put_outputs(ctx.conn, ctx.run_id, "roster", [RosterEntry(
        mention_id="nm:04267576:r6", meeting_id=MTG, resolution_no=6, name="André Lacroix",
        coverage="structured")])
    db.put_outputs(ctx.conn, ctx.run_id, "seat", [
        seat(1, "Intertek Group plc", "Chief Executive Officer", mention="nm:04267576:r6")])
    research.run(ctx)
    (s,) = db.get_outputs(ctx.conn, ctx.run_id, "seat")
    assert (s.role_type, s.board_seat, s.executive) == ("officer_title", "yes", "yes")


def test_analyst_org_override_reaches_seat_listed(online) -> None:
    ctx, answers, _ = online
    answers["org"] = lambda _: payload({"equity_listed": fact("unknown", None, None)})
    db.put_outputs(ctx.conn, ctx.run_id, "seat", [seat(1, "Moelis & Company", "Partner")])
    db.insert_decision(ctx.conn, "set_fact", {"kind": "org", "target": "moelis-and-company",
                                              "field": "listed", "value": "yes"},
                       "expert", "NYSE: MC")
    research.run(ctx)
    (org,) = db.get_outputs(ctx.conn, ctx.run_id, "org")
    (s,) = db.get_outputs(ctx.conn, ctx.run_id, "seat")
    assert (org.listed, org.equity_listing, s.listed) == ("yes", "yes", "yes")
    assert org.evidence[-1].source == "analyst" and org.evidence[-1].ref == "dec:1"


# --- review fixes: unknown market segment, unknown debt listing, analyst listed ---------

@pytest.mark.parametrize("segment", [None, fact("other"), fact("main", url="https://not.returned")])
@pytest.mark.parametrize(("aim", "want"), [("yes", "yes"), ("no", "unknown"),
                                           ("unknown", "unknown")])
def test_unknown_market_segment_is_not_a_main_market(online, segment, aim, want) -> None:
    """Equity listed but the segment unsourced (missing, 'other', or dropped as unsourced):
    it could be AIM, so the seat is listed only if AIM counts; never yes by default."""
    ctx, answers, _ = online
    answer = {"equity_listed": fact("yes"), "exchange": fact("London Stock Exchange")}
    answers["org"] = lambda _: payload(answer | ({"market_segment": segment} if segment else {}))
    org = research_org("IG Design Group plc", "2026-06-18", ctx)
    assert org.equity_listing == "yes" and "segment unknown" in org.market
    assert seat_listed(org, Policy(aim_counts_as_listed=aim)) == want


def test_known_main_market_is_listed(online) -> None:
    ctx, answers, _ = online
    answers["org"] = lambda _: payload({"equity_listed": fact("yes"), "exchange": fact("NYSE"),
                                        "market_segment": fact("main")})
    org = research_org("Capri Holdings Limited", "2026-05-20", ctx)
    assert org.market == "NYSE" and seat_listed(org, Policy(aim_counts_as_listed="no")) == "yes"


@pytest.mark.parametrize(("equity", "debt", "listed"), [
    ("no", None, "unknown"), ("no", "unknown", "unknown"), ("no", "no", "no"),
    ("no", "yes", "yes"), ("yes", None, "yes"), ("unknown", "no", "unknown")])
def test_unknown_debt_listing_is_not_coerced_to_no(online, equity, debt, listed) -> None:
    ctx, answers, _ = online
    answer = {"equity_listed": fact(equity), "market_segment": fact("main")}
    answers["org"] = lambda _: payload(answer | ({"debt_listed": fact(debt)} if debt else {}))
    org = research_org("Some Company plc", "2026-06-18", ctx)
    assert (org.equity_listing, org.listed) == (equity, listed)
    if equity == "no":  # debt-only listings don't count by default, so the seat is still no
        assert seat_listed(org, Policy()) == "no"


@pytest.mark.parametrize("value", ["yes", "no"])
def test_analyst_listed_beats_the_aim_flag_in_classify(online, value) -> None:
    """An analyst's org `listed` reaches the seat as given; the AIM flag doesn't re-apply."""
    ctx, answers, _ = online
    answers["org"] = lambda _: payload({"equity_listed": fact("yes"), "exchange": fact("LSE"),
                                        "market_segment": fact("aim")})
    db.put_outputs(ctx.conn, ctx.run_id, "seat", [
        seat(1, "IG Design Group plc", "Interim Executive Chair")])
    db.insert_decision(ctx.conn, "set_fact", {"kind": "org", "target": "ig-design-group",
                                              "field": "listed", "value": value},
                       "expert", "checked")
    research.run(ctx)
    (s,) = db.get_outputs(ctx.conn, ctx.run_id, "seat")
    assert s.listed == value
