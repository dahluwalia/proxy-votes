"""M6.1 reconcile: one test per row of the status table, plus org-name folding."""

from __future__ import annotations

from datetime import date

from pennant.core.models import (
    CHAppointment,
    CHOfficer,
    Meeting,
    OrgFacts,
    Person,
    Seat,
    SeatStatus,
)
from pennant.policy.reconcile import group_key, norm_org, org_index, reconcile

TESCO = Meeting(meeting_id="mtg:00445790:2026-06-18", company_name="Tesco PLC",
                company_number="00445790", agm_date=date(2026, 6, 18), doc_sha="x", key="tesco")
MENTION = "nm:00445790:r7"
PERSON = Person(person_id=f"p:{MENTION}", mention_ids=[MENTION], officer_ids=["off-1"])


def seat(org: str, **facts) -> Seat:
    base = dict(board_seat="yes", chair="no", executive="no", current="yes")
    return Seat(seat_id=f"seat:{MENTION}:{norm_org(org)}", mention_id=MENTION, org_name=org,
                org_key=norm_org(org), **(base | facts))


def officer(*appts: tuple) -> list[CHOfficer]:
    """appts: (number, name, type, appointed, resigned)."""
    return [CHOfficer(officer_id="off-1", name="GARNIER, Thierry", appointments=[
        CHAppointment(company_number=n, company_name=c, company_type=t, role="director",
                      appointed_on=a, resigned_on=r) for n, c, t, a, r in appts])]


ISSUER = ("00445790", "TESCO PLC", "plc", date(2023, 5, 1), None)


def run(seats, ch, orgs=()):
    out, items = reconcile(PERSON, TESCO, seats, ch, orgs)
    return {s.org_name: s for s in out}, items


def test_confirmed_sets_board_seat_for_a_job_title():
    seats, items = run([seat("Tesco PLC"), seat("Kingfisher plc", board_seat="unknown",
                                                 executive="yes", role_type="officer_title")],
                       officer(ISSUER, ("01664812", "KINGFISHER PLC", "plc", date(2019, 9, 25),
                                        None)))
    assert seats["Kingfisher plc"].status == SeatStatus.confirmed
    assert seats["Kingfisher plc"].board_seat == "yes"
    assert any(e.source == "ch" for e in seats["Kingfisher plc"].evidence)
    assert seats["Tesco PLC"].status == SeatStatus.confirmed
    assert items == []


def test_confirmed_keeps_a_role_that_is_not_a_board_seat():
    """A charity's trustees are CH directors; the trustee role still isn't a counted seat."""
    seats, _ = run([seat("The Burberry Foundation", board_seat="no", role_type="trustee")],
                   officer(("08305566", "THE BURBERRY FOUNDATION", "private-limited-guarant-nsc",
                            date(2018, 5, 2), None)))
    assert seats["The Burberry Foundation"].status == SeatStatus.confirmed
    assert seats["The Burberry Foundation"].board_seat == "no"


def test_stale_when_ch_shows_a_resignation_before_the_agm():
    seats, items = run([seat("Great Portland Estates Plc")],
                       officer(("00596137", "GREAT PORTLAND ESTATES P.L.C.", "plc",
                                date(2016, 10, 1), date(2026, 4, 30))))
    s = seats["Great Portland Estates Plc"]
    assert (s.status, s.current) == (SeatStatus.stale, "unknown")
    assert [i.kind for i in items] == ["stale"] and items[0].subject == s.seat_id
    assert "set-fact" in items[0].suggested_command


def test_contradiction_when_a_uk_company_shows_no_appointment():
    seats, items = run([seat("Tesco PLC"), seat("Whitbread PLC", ch_company_number="29423")],
                       officer(ISSUER))
    s = seats["Whitbread PLC"]
    assert (s.status, s.current) == (SeatStatus.contradiction, "unknown")
    assert [i.kind for i in items] == ["contradiction"]


def test_non_uk_org_is_ch_invisible_and_keeps_notice_facts():
    seats, items = run([seat("Tesco PLC"), seat("Exor N.V.")], officer(ISSUER))
    s = seats["Exor N.V."]
    assert (s.status, s.board_seat, s.current) == (SeatStatus.ch_invisible, "yes", "yes")
    assert items == []


def test_ch_unavailable_leaves_a_job_title_without_a_board_seat():
    seats, items = run([seat("Kingfisher plc", board_seat="unknown", role_type="officer_title"),
                        seat("Diageo PLC")], None)
    assert seats["Kingfisher plc"].status == SeatStatus.ch_unavailable
    assert seats["Kingfisher plc"].board_seat == "unknown"
    assert seats["Diageo PLC"].board_seat == "yes"  # the role says board
    assert items == []


def test_past_beats_ch_and_is_not_reported_as_undisclosed():
    seats, items = run([seat("Standard Chartered PLC", current="no")],
                       officer(("00966425", "STANDARD CHARTERED PLC", "plc", date(2013, 9, 1),
                                None)))
    assert seats["Standard Chartered PLC"].status == SeatStatus.past
    assert len(seats) == 1 and items == []


def test_undisclosed_ch_directorship_becomes_a_seat_and_a_review_item():
    seats, items = run([], officer(
        ("00002065", "LLOYDS BANK PLC", "plc", date(2019, 3, 1), None),
        ("04155426", "NATURE'S WAY FOODS LIMITED", "ltd", date(2019, 1, 1), None),  # private
        ("00596137", "OLD CO PLC", "plc", date(2010, 1, 1), date(2020, 1, 1))))  # resigned
    assert list(seats) == ["LLOYDS BANK PLC"]
    s = seats["LLOYDS BANK PLC"]
    assert (s.status, s.board_seat, s.chair, s.executive) == (
        SeatStatus.undisclosed, "yes", "unknown", "unknown")
    assert s.listed == "unknown" and s.officer_id == "off-1" and s.mention_id is None
    assert [i.kind for i in items] == ["undisclosed"]


def test_undisclosed_skips_an_org_research_found_unlisted():
    orgs = [OrgFacts(org_key="bos", canonical_name="Bank of Scotland plc", listed="no")]
    seats, _ = run([], officer(("SC327000", "BANK OF SCOTLAND PLC", "plc", None, None)), orgs)
    assert seats == {}


def test_unknown_role_facts_fill_from_another_notice_about_the_same_seat():
    ms = seat("Marks and Spencer Group plc", chair="unknown", executive="unknown",
              role_type="other")
    other = ms.model_copy(update={"seat_id": ms.seat_id + "-2", "chair": "no", "executive": "no"})
    seats, _ = reconcile(PERSON, TESCO, [ms, other], None)
    assert {(s.chair, s.executive) for s in seats} == {("no", "no")}


def test_issuer_seat_is_listed_and_current():
    seats, _ = run([seat("Tesco PLC", current="unknown")], officer(ISSUER))
    assert (seats["Tesco PLC"].listed, seats["Tesco PLC"].current) == ("yes", "yes")
    assert seats["Tesco PLC"].ch_company_number == "00445790"


def test_issuer_seat_stays_unknown_without_a_ch_record():
    """Standing at the AGM implies a board seat only if CH can confirm the person: Thierry
    Garnier left Tesco's board before its AGM, so with no CH record his seat is unknown."""
    seats, _ = run([seat("Tesco PLC", current="unknown")], None)
    assert (seats["Tesco PLC"].listed, seats["Tesco PLC"].current) == ("yes", "unknown")


def test_norm_org_folds_suffixes_case_and_punctuation():
    assert norm_org("GREAT PORTLAND ESTATES P.L.C.") == norm_org("Great Portland Estates Plc")
    assert norm_org("Tate & Lyle PLC") == "tate and lyle"
    assert norm_org("HAFREN DYFRDWY CYFYNGEDIG") == "hafren dyfrdwy"
    assert norm_org("HALMA PUBLIC LIMITED COMPANY") == norm_org("Halma Plc") == "halma"
    assert norm_org("The Burberry Foundation") != norm_org("Burberry Group plc")


def test_group_key_follows_parents_and_never_loops():
    orgs = [OrgFacts(org_key="icee", canonical_name="ICE Clear Europe Limited",
                     parent_name="Intercontinental Exchange, Inc."),
            OrgFacts(org_key="a", canonical_name="A plc", parent_name="B plc"),
            OrgFacts(org_key="b", canonical_name="B plc", parent_name="A plc")]
    idx = org_index(orgs)
    assert group_key(seat("ICE Clear Europe"), idx) == "intercontinental exchange"
    assert group_key(seat("ICE Clear Europe"), idx, dedupe=False) == "ice clear europe"
    assert group_key(seat("A plc"), idx) in {"a", "b"}


def test_appointment_starting_after_the_agm_is_not_stale():
    """A CH appointment that begins after the AGM is no resignation: current unknown, and the
    review item says it was appointed after the AGM."""
    later = ("01664812", "KINGFISHER PLC", "plc", date(2026, 9, 1), None)
    seats, items = run([seat("Tesco PLC"), seat("Kingfisher plc")], officer(ISSUER, later))
    s = seats["Kingfisher plc"]
    assert (s.status, s.current) == (SeatStatus.contradiction, "unknown")
    (item,) = items
    assert "appointed after the AGM (2026-09-01)" in item.detail
    assert "resigned" not in item.detail
