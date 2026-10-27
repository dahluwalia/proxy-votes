"""Citation dates and the stale-evidence flag: checked before min(today, AGM date)."""

from __future__ import annotations

from datetime import date, datetime

from pennant.ch.client import BASE_URL, WEB_URL
from pennant.core import db
from pennant.core.cache import put_cache
from pennant.core.models import (
    Evidence,
    Meeting,
    OrgFacts,
    Person,
    RosterEntry,
    Seat,
)
from pennant.evidence import freshness
from pennant.evidence.cite import cite

OFFICER = "abcDEF_123-xyzXYZ_456789"
APPTS = f"/officers/{OFFICER}/appointments"
COMPANY = "/company/06219884"


def at(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T12:00:00+00:00")


def page(conn, path: str, start: int, total: int, day: str, checked: str | None = None):
    url = f"{BASE_URL}{path}?items_per_page=50&start_index={start}"
    put_cache(conn, "ch", url, {"status": 200, "body": {"total_results": total}}, at(day))
    if checked:
        with conn:
            conn.execute("UPDATE cache SET last_fetched = ? WHERE key = ?",
                         (at(checked).isoformat(), url))


def ch(path: str, **dates) -> Evidence:
    return Evidence(source="ch", ref=WEB_URL + path, quote="q", **dates)


# --- the rule -------------------------------------------------------------------


def test_need_from_is_the_earlier_of_today_and_the_latest_agm():
    today = date(2026, 10, 27)
    assert freshness.need_from([date(2026, 5, 7)], today) == date(2026, 5, 7)
    assert freshness.need_from([date(2027, 5, 7)], today) == today
    assert freshness.need_from([date(2026, 5, 7), date(2026, 6, 18)], today) == date(2026, 6, 18)
    assert freshness.need_from([], today) is None


def test_stale_uses_the_last_check_not_the_last_change():
    need = date(2026, 5, 7)
    assert freshness.is_stale(ch(APPTS, fetched_at=at("2026-05-06")), need)
    assert not freshness.is_stale(ch(APPTS, fetched_at=at("2026-05-07")), need)
    unchanged_but_rechecked = ch(APPTS, fetched_at=at("2026-04-01"), checked_at=at("2026-05-08"))
    assert not freshness.is_stale(unchanged_but_rechecked, need)
    assert freshness.is_stale(ch(APPTS), need)  # undated fetched data fails closed
    web = Evidence(source="web", ref="https://x.example", fetched_at=at("2026-05-01"))
    assert freshness.is_stale(web, need)


def test_notice_and_analyst_citations_are_never_stale():
    need = date(2026, 5, 7)
    assert not freshness.is_stale(Evidence(source="notice", ref="ITV.pdf#page=3"), need)
    assert not freshness.is_stale(Evidence(source="analyst", ref="dec:1"), need)


# --- dates from the cache -------------------------------------------------------


def test_paged_list_is_as_fresh_as_its_oldest_page_ignoring_leftover_pages(ctx):
    page(ctx.conn, APPTS, 0, 60, "2026-04-01", checked="2026-10-26")
    page(ctx.conn, APPTS, 50, 60, "2026-09-01", checked="2026-10-25")
    page(ctx.conn, APPTS, 100, 120, "2025-01-01")  # from when the list was longer
    updated, checked = freshness.ch_dates(ctx.conn, WEB_URL + APPTS)
    assert updated.date() == date(2026, 9, 1)
    assert checked.date() == date(2026, 10, 25)


def test_company_profile_ref_does_not_pick_up_its_officer_list(ctx):
    put_cache(ctx.conn, "ch", BASE_URL + COMPANY, {"status": 200, "body": {}}, at("2026-10-20"))
    page(ctx.conn, COMPANY + "/officers", 0, 5, "2026-01-01")
    updated, checked = freshness.ch_dates(ctx.conn, WEB_URL + COMPANY)
    assert updated.date() == checked.date() == date(2026, 10, 20)
    assert freshness.ch_dates(ctx.conn, WEB_URL + "/company/99999999") is None
    assert freshness.ch_dates(ctx.conn, "https://example.com/x") is None


# --- the stage ------------------------------------------------------------------


def seed(ctx, agm: date) -> None:
    mtg = "mtg:00445790:" + agm.isoformat()
    db.put_outputs(ctx.conn, ctx.run_id, "meeting", [Meeting(
        meeting_id=mtg, company_name="Tesco PLC", company_number="00445790",
        agm_date=agm, doc_sha="x")])
    db.put_outputs(ctx.conn, ctx.run_id, "roster", [RosterEntry(
        mention_id="nm:00445790:r13", meeting_id=mtg, resolution_no=13,
        name="Caroline Silver", coverage="structured")])
    db.put_outputs(ctx.conn, ctx.run_id, "person", [Person(
        person_id="p:nm:00445790:r13", mention_ids=["nm:00445790:r13"],
        officer_ids=[OFFICER])])
    db.put_outputs(ctx.conn, ctx.run_id, "seat", [Seat(
        seat_id="seat:00445790:r13:2", mention_id="nm:00445790:r13", org_name="ICE Clear Europe",
        org_key="ice-clear-europe", status="confirmed",
        evidence=[Evidence(source="notice", ref="Tesco.pdf#page=10", quote="Chair"),
                  ch(APPTS)])])
    db.put_outputs(ctx.conn, ctx.run_id, "org", [OrgFacts(
        org_key="ice-clear-europe", canonical_name="ICE Clear Europe", listed="no",
        evidence=[ch(COMPANY)])])
    page(ctx.conn, APPTS, 0, 7, "2026-04-01", checked="2026-05-01")
    put_cache(ctx.conn, "ch", BASE_URL + COMPANY, {"status": 200, "body": {}}, at("2026-10-26"))


def test_stage_dates_citations_and_flags_data_checked_before_the_agm(ctx):
    seed(ctx, date(2026, 6, 18))
    report = freshness.run(ctx, today=date(2026, 10, 27))
    assert report.ok and report.counts["stale_citations"] == 1
    seat = db.get_outputs(ctx.conn, ctx.run_id, "seat")[0]
    notice, appts = seat.evidence
    assert notice.fetched_at is None
    assert appts.fetched_at.date() == date(2026, 4, 1)
    assert appts.checked_at.date() == date(2026, 5, 1)
    [item] = db.get_outputs(ctx.conn, ctx.run_id, "review")
    assert (item.kind, item.subject) == ("stale_evidence", "p:nm:00445790:r13")
    assert "last checked 2026-05-01, before the AGM on 2026-06-18" in item.detail
    assert freshness.flagged([item]) == {("p:nm:00445790:r13", WEB_URL + APPTS)}
    org = db.get_outputs(ctx.conn, ctx.run_id, "org")[0]
    assert org.evidence[0].checked_at.date() == date(2026, 10, 26)  # dated, not stale


def test_an_agm_still_ahead_needs_data_checked_today(ctx):
    seed(ctx, date(2027, 6, 18))
    freshness.run(ctx, today=date(2026, 10, 27))
    details = sorted(i.detail for i in db.get_outputs(ctx.conn, ctx.run_id, "review"))
    assert len(details) == 2  # the appointments (May) and the company profile (yesterday)
    assert all("before today (2026-10-27), and the AGM on 2027-06-18 is still ahead" in d
               for d in details)


def test_fresh_data_raises_nothing(ctx):
    seed(ctx, date(2026, 4, 30))
    assert freshness.run(ctx, today=date(2026, 10, 27)).counts["stale_citations"] == 0


# --- citation format ------------------------------------------------------------


def test_cite_shows_last_updated_and_last_checked():
    url = WEB_URL + APPTS
    same = ch(APPTS, fetched_at=at("2026-10-26"), checked_at=at("2026-10-26"))
    assert cite(same) == f'Companies House: {url} — "q" (updated 2026-10-26)'
    later = ch(APPTS, fetched_at=at("2026-04-01"), checked_at=at("2026-10-26"))
    assert cite(later).endswith("(updated 2026-04-01, checked 2026-10-26)")
    web = Evidence(source="web", ref="https://x.example", fetched_at=at("2026-10-20"))
    assert cite(web) == "Web: https://x.example (updated 2026-10-20)"
