"""Roles from a firm's own website: finding the site, keeping answers on it, filling seats."""

from __future__ import annotations

import pytest

from pennant.classify import positions
from pennant.classify.positions import merge_position, research_position, research_site
from pennant.core import db
from pennant.core.models import Evidence, PositionFacts, Seat
from tests.classify import test_research
from tests.classify.test_research import fact, payload

online = test_research.online  # the fake research model
SITE = "https://corporate.example-retail.com"
PAGE = SITE + "/about-us"


@pytest.mark.parametrize("host, domain", [
    ("corporate.marksandspencer.com", "marksandspencer.com"),
    ("www.tesco.co.uk", "tesco.co.uk"),
    ("investors.example.com.au", "example.com.au"),
    ("fedex.com", "fedex.com"),
])
def test_site_domain_keeps_the_registrable_part(host, domain):
    assert positions.site_domain(host) == domain


def _site_answer(website, url, quote):
    return lambda _: payload({"website": fact(website, url, quote)}, urls=(url,))


def test_own_site_is_accepted(online):
    ctx, answers, _ = online
    answers["site"] = _site_answer(SITE, PAGE, "Example Retail Group plc is a British retailer")
    domain, ev = research_site("Example Retail Group plc", ctx)
    assert domain == "example-retail.com" and ev.ref == PAGE


@pytest.mark.parametrize("website, url, quote", [
    ("https://en.wikipedia.org", "https://en.wikipedia.org/wiki/X", "Example Retail Group plc"),
    (SITE, "https://news.example.org/story", "Example Retail Group plc"),  # cited page off site
    # neither the page nor the site's address names the firm
    ("https://www.acme-holdings.com", "https://www.acme-holdings.com/about", "A British retailer"),
    ("unknown", None, None),
])
def test_anything_but_a_cited_own_site_is_refused(online, website, url, quote):
    ctx, answers, _ = online
    answers["site"] = (_site_answer(website, url, quote) if url
                       else lambda _: payload({"website": fact("unknown", None, None)}))
    assert research_site("Example Retail Group plc", ctx) == (None, None)
    [review] = db.get_outputs(ctx.conn, ctx.run_id, "review")
    assert review.kind == "site_not_found" and "roles there stay unknown" in review.detail


def test_role_question_searches_only_the_site_and_drops_off_site_facts(online):
    ctx, answers, calls = online
    board = SITE + "/board"
    answers["position"] = lambda _: payload({
        "capacity": fact("non_executive_director", board, "Jane Smith, Non-executive Director"),
        "end_date": fact("2026-01-31", "https://press.example.org/x", "Jane Smith stepped down"),
    }, urls=(board, "https://press.example.org/x"))
    pf = research_position("Jane Smith", "Example Retail Group plc", "director", "2026-06-18",
                           "example-retail.com", ctx, "seat:o1:01234567")
    assert calls[-1]["domains"] == ["example-retail.com"]
    assert (pf.board_seat, pf.chair, pf.executive, pf.current) == ("yes", "no", "no", "yes")
    assert [e.ref for e in pf.evidence] == [board]  # the off-site end date was dropped
    [review] = db.get_outputs(ctx.conn, ctx.run_id, "review")
    assert review.kind == "web_unsourced" and "not on example-retail.com" in review.detail


def test_a_job_title_with_no_board_listing_stays_unknown(online):
    ctx, answers, _ = online
    answers["position"] = lambda _: payload({"board_member": fact("unknown", None, None)})
    pf = research_position("Kawal Preet", "FedEx", "Executive Vice President", "2026-05-20",
                           "fedex.com", ctx, "s2")
    assert (pf.board_seat, pf.chair, pf.executive) == ("unknown", "unknown", "unknown")


def _seat(**kw):
    base = dict(seat_id="seat:o1:01234567", officer_id="o1", org_name="EXAMPLE PLC",
                org_key="example", role_text="director", role_type="director_unspecified",
                board_seat="yes", current="yes", listed="yes", identity="yes")
    return Seat(**(base | kw))


def test_site_answers_fill_only_unknown_facts():
    ev = Evidence(source="web", ref=SITE + "/board", quote="Non-executive Director")
    pf = PositionFacts(seat_id="seat:o1:01234567", board_seat="no", chair="no", executive="no",
                       role_type="non_executive_director", evidence=[ev])
    merged = merge_position(_seat(), pf)
    assert merged.board_seat == "yes"  # Companies House said so: the site can't undo it
    assert (merged.chair, merged.executive, merged.role_type) == (
        "no", "no", "non_executive_director")
    assert merged.evidence == [ev]
    known = _seat(chair="yes", executive="no")
    assert merge_position(known, pf) == known.model_copy(
        update={"role_type": "non_executive_director", "evidence": [ev]})
    assert merge_position(known, PositionFacts(seat_id=known.seat_id)) == known


def test_needs_position_only_for_seats_that_could_count():
    assert positions.needs_position(_seat())
    assert not positions.needs_position(_seat(chair="no", executive="no"))
    assert not positions.needs_position(_seat(listed="no"))
    assert not positions.needs_position(_seat(current="no"))
    assert not positions.needs_position(_seat(role_type="trustee", board_seat="unknown"))


def test_role_is_asked_as_of_the_agm(online):
    """The vote is due by the AGM, so the question is about that date: a role that ended
    before it, or began after it, is not current, whatever the site shows today."""
    ctx, answers, calls = online
    board = SITE + "/board"
    answers["position"] = lambda _: payload({
        "capacity": fact("non_executive_director", board, "Jane Smith, Non-executive Director"),
        "start_date": fact("2026-09-01", board, "Jane Smith joined the Board on 1 September 2026"),
    }, urls=(board,))
    pf = research_position("Jane Smith", "Example Retail Group plc", "director", "2026-06-18",
                           "example-retail.com", ctx, "seat:o1:01234567")
    assert calls[-1]["inputs"]["as_of"] == "2026-06-18"
    assert pf.current == "unknown"  # joined after the AGM
