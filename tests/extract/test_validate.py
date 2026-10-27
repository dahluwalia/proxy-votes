"""M1.2: validate the recorded extracts against the PDFs and record meeting, roster, seats."""

from __future__ import annotations

import shutil
from datetime import date

import pytest

from pennant.core import db
from pennant.core.config import NOTICES_DIR, notices
from pennant.evidence.grounding import load_pages
from pennant.extract import meeting, validate
from pennant.extract.llamaextract import extract_notice
from pennant.extract.schema import ExtractedAppointment

MEETINGS = {  # key: (company, number, AGM date, first and last resolution)
    "itv": ("ITV plc", "04967001", date(2026, 5, 7), 4, 14),
    "intertek": ("Intertek Group plc", "04267576", date(2026, 5, 20), 5, 15),
    "tesco": ("Tesco PLC", "00445790", date(2026, 6, 18), 4, 14),
    "marks-and-spencer": ("Marks and Spencer Group plc", "04256886", date(2026, 7, 7), 5, 13),
    "severn-trent": ("Severn Trent Plc", "02366619", date(2026, 7, 9), 5, 12),
}
NOT_STANDING = ["Andrew Martin", "Gurnek Bains", "Alison Platt", "Liv Garfield", "Anita Anand",
                "Nick Folland", "Kyla Mullins", "Didar Dhillon", "Ida Woodger", "Chris Taylor"]


@pytest.fixture
def stage(ctx):
    report = validate.run(ctx)
    out = {k: db.get_outputs(ctx.conn, ctx.run_id, k) for k in ("meeting", "roster", "seat",
                                                                 "review")}
    return report, out


def seats_of(out, name):
    entry = next(e for e in out["roster"] if name in e.name)
    return entry, {s.org_name: s for s in out["seat"] if s.mention_id == entry.mention_id}


def tesco(ctx):
    return extract_notice(NOTICES_DIR / notices()["tesco"], ctx), load_pages("tesco")


# --- Accept, on the recorded fixtures (offline) -----------------------------


def test_meeting_table_offline(stage):
    report, out = stage
    assert report.ok, report.errors
    got = {m.key: m for m in out["meeting"]}
    assert set(got) == set(MEETINGS)
    for key, (company, number, day, first, last) in MEETINGS.items():
        m = got[key]
        assert (m.company_name, m.company_number, m.agm_date) == (company, number, day)
        assert m.meeting_id == f"mtg:{number}:{day}"
        nums = sorted(e.resolution_no for e in out["roster"] if e.meeting_id == m.meeting_id)
        assert nums == list(range(first, last + 1)), key


def test_fifty_roster_entries_and_nobody_else(stage):
    _, out = stage
    assert len(out["roster"]) == 50
    names = " | ".join(e.name for e in out["roster"])
    for person in NOT_STANDING:
        assert person not in names


def test_zero_ungrounded_quotes_on_the_fixtures(stage):
    report, out = stage
    assert report.counts["ungrounded"] == 0
    assert [r for r in out["review"] if r.kind.startswith("ungrounded")] == []


def test_every_roster_entry_has_an_issuer_seat_and_cited_seats(stage):
    _, out = stage
    issuer = {s.mention_id for s in out["seat"] if s.seat_id.endswith(":0")}
    assert issuer == {e.mention_id for e in out["roster"]}
    for s in out["seat"]:
        assert s.evidence and s.evidence[0].source == "notice" and "#page=" in s.evidence[0].ref
        assert s.identity == "yes"


def test_hard_case_seats(stage):
    _, out = stage
    silver, seats = seats_of(out, "Caroline Silver")
    assert seats["Barratt Redrow plc"].chair == "yes"
    assert seats["ICE Clear Europe"].chair == "yes"
    assert seats["International Advisory Board of Adobe Inc"].role_type == "adviser"
    assert seats["board of trustees of V&A Foundation"].role_type == "trustee"
    assert seats["Moelis & Company"].board_seat == "unknown"
    assert len(seats) == 8  # Tesco plus 7 appointments
    _, hodgson = seats_of(out, "Christine Hodgson")
    assert next(s for o, s in hodgson.items() if "Standard Chartered" in o).current == "no"
    _, lacroix = seats_of(out, "André Lacroix")
    assert next(s for o, s in lacroix.items() if "Reckitt" in o).current == "no"


def test_ms_entries_have_no_biography(stage):
    _, out = stage
    ms = next(m for m in out["meeting"] if m.key == "marks-and-spencer")
    entries = [e for e in out["roster"] if e.meeting_id == ms.meeting_id]
    assert len(entries) == 9 and {e.coverage for e in entries} == {"none"}


# --- fail closed --------------------------------------------------------------


def test_fabricated_appointment_is_dropped_and_reviewed(ctx):
    ex, pages = tesco(ctx)
    silver = next(d for d in ex.directors if d.name == "Caroline Silver")
    fake = ExtractedAppointment(
        organisation="Standard Chartered", role="Non-executive Director", tense="current",
        section="current_appointments", quote="Non-executive Director of Standard Chartered",
        evidence=silver.appointments[0].evidence)
    silver.appointments.append(fake)
    res = validate.check_notice(ex, pages, ctx.policy)
    assert res.errors == []
    assert not any(s.org_name == "Standard Chartered" for s in res.seats)
    [item] = [r for r in res.reviews if r.kind == "ungrounded_quote"]
    assert item.subject == "nm:00445790:r13" and "Standard Chartered" in item.detail


def test_real_quote_naming_another_organisation_is_dropped(ctx):
    ex, pages = tesco(ctx)
    silver = next(d for d in ex.directors if d.name == "Caroline Silver")
    silver.appointments[0].organisation = "Lloyds Banking Group plc"
    res = validate.check_notice(ex, pages, ctx.policy)
    assert [r.kind for r in res.reviews] == ["ungrounded_quote"]
    assert "does not name" in res.reviews[0].detail


def test_gap_in_resolution_numbers_is_a_stage_error(ctx):
    ex, pages = tesco(ctx)
    ex.directors[3].resolution_no = 30
    res = validate.check_notice(ex, pages, ctx.policy)
    assert res.meeting is None and res.roster == []
    assert "not contiguous" in res.errors[0]


@pytest.mark.parametrize("field, value, message", [
    ("company_number", "04967001", "not in the PDF"),
    ("company_number", "unknown", "is not a number"),
    ("agm_date", "2027-08-07", "AGM date"),
    ("agm_date", None, "AGM date"),
])
def test_meeting_facts_must_be_in_the_pdf(ctx, field, value, message):
    ex, pages = tesco(ctx)
    res = validate.check_notice(ex.model_copy(update={field: value}), pages, ctx.policy)
    assert res.meeting is None and message in res.errors[0]


def test_director_not_named_in_the_pdf_is_a_stage_error(ctx):
    ex, pages = tesco(ctx)
    ex.directors[0].name = "Jane Invented"
    res = validate.check_notice(ex, pages, ctx.policy)
    assert any("Jane Invented" in e for e in res.errors)


def test_role_not_in_the_pdf_is_treated_as_unstated(ctx):
    ex, pages = tesco(ctx)
    ex.directors[0].role_at_company = "Executive Vice Chair of Everything"
    res = validate.check_notice(ex, pages, ctx.policy)
    entry = res.roster[0]
    assert entry.role_text_at_issuer is None and entry.role_type_at_issuer is None
    assert [r.kind for r in res.reviews] == ["ungrounded_role"]


def test_missing_recording_offline_fails_the_stage(ctx, tmp_path):
    shutil.copy(NOTICES_DIR / notices()["tesco"], tmp_path / "Unrecorded_Notice.pdf")
    report = validate.run(ctx, notices_dir=tmp_path)
    assert not report.ok and "nothing recorded" in report.errors[0]
    assert db.get_outputs(ctx.conn, ctx.run_id, "roster") == []


# --- text checks ---------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(MEETINGS))
def test_number_and_agm_date_read_from_the_text(key):
    _, number, day, _, _ = MEETINGS[key]
    pages = load_pages(key)
    assert meeting.text_company_numbers(pages) == {number}
    assert day in meeting.text_agm_dates(pages)
    assert not any(d.year == 2027 for d in meeting.text_agm_dates(pages))


def test_gap_problem():
    assert meeting.gap_problem([5, 4, 6]) is None
    assert "contiguous" in meeting.gap_problem([4, 6])
    assert "contiguous" in meeting.gap_problem([4, 4, 5])
    assert meeting.gap_problem([]) == "no directors standing"
    assert "no resolution number" in meeting.gap_problem([4, None])


# --- review fixes ----------------------------------------------------------------


def whitworth_rank(ex):
    whitworth = next(d for d in ex.directors if d.name == "Karen Whitworth")
    return next(a for a in whitworth.appointments if a.organisation == "The Rank Group plc")


@pytest.mark.parametrize("in_quote_too", [False, True])
def test_organisation_must_be_on_the_page_exactly_not_fuzzily(ctx, in_quote_too):
    ex, pages = tesco(ctx)
    rank = whitworth_rank(ex)
    rank.organisation = "The Bank Group plc"
    if in_quote_too:  # the quote still grounds fuzzily on p.10, but the page says "Rank"
        rank.quote = rank.quote.replace("Rank", "Bank")
        assert validate.ground_near(pages, 10, rank.quote)[1] == "fuzzy"
    res = validate.check_notice(ex, pages, ctx.policy)
    assert not any(s.org_name == "The Bank Group plc" for s in res.seats)
    [item] = [r for r in res.reviews if r.kind == "ungrounded_quote"]
    assert item.subject == "nm:00445790:r14" and "Bank Group" in item.detail


@pytest.mark.parametrize("name, role", [
    ("Ken Murphy", "Independent Non-executive Director"),  # printed for others on p.9
    ("Ken Murphy", "Chair"),  # Gerry Murphy's role, on Ken Murphy's own page
    ("Melissa Bethell", "Executive Director"),  # only inside "Non-executive Director"
])
def test_role_must_follow_the_directors_own_name(ctx, name, role):
    ex, pages = tesco(ctx)
    d = next(d for d in ex.directors if d.name == name)
    d.role_at_company = role
    res = validate.check_notice(ex, pages, ctx.policy)
    entry = next(e for e in res.roster if e.name == name)
    assert entry.role_text_at_issuer is None and entry.role_type_at_issuer is None
    issuer = next(s for s in res.seats if s.seat_id.endswith(":0")
                  and s.mention_id == entry.mention_id)
    assert issuer.role_type == "other" and issuer.executive == "unknown"
    assert [r.kind for r in res.reviews] == ["ungrounded_role"]


def test_role_found_after_the_name_on_an_uncited_page(stage):
    _, out = stage
    doyle = next(e for e in out["roster"] if e.name == "Sean Doyle")  # role cited on p.7 only
    assert doyle.role_type_at_issuer == "non_executive_director"
    assert "#page=4" in doyle.evidence[1].ref  # "Roger Burnley and Sean Doyle joined ... as NEDs"


@pytest.mark.parametrize("org, role, role_type, board_seat", [
    ("Mentor Graphics Corporation", "Non-executive director", "non_executive_director", "yes"),
    ("the Almeida Theatre Board of Trustees", "Chair", "trustee", "no"),  # chairs trustees
    ("Advisory Board Company plc", "Chief executive officer", "officer_title", "unknown"),
    ("International Advisory Board of Adobe Inc", "Other", "adviser", "no"),
    ("board of trustees of V&A Foundation", "Other", "trustee", "no"),
    ("Frontier Economics Advisory Council", "Director", "adviser", "no"),
    ("Mentor Graphics Corporation", "Other", "other", "unknown"),
])
def test_organisation_name_only_overrides_a_generic_role(ctx, org, role, role_type, board_seat):
    a = ExtractedAppointment(organisation=org, role=role, tense="current",
                             section="current_appointments", quote=f"{role}: {org}")
    rf = validate.appointment_facts(a, ctx.policy)
    assert (rf.role_type, rf.board_seat) == (role_type, board_seat)


@pytest.mark.parametrize("text, number", [
    ("Registered in Scotland No. SC083026", "SC083026"),
    ("Registered in England and Wales No. 04 967 001", "04967001"),
    ("Company number: NI 012345", "NI012345"),
    ("Registered in England and Wales: 445790", "00445790"),
])
def test_company_number_with_a_prefix_or_spaces(text, number):
    assert meeting.text_company_numbers(["Notice of AGM", text]) == {number}


def _fake_notices(monkeypatch, ex, pages, broken=()):
    """extract_notice and page_texts stubbed: a 2025 notice is the 2026 one a year earlier."""
    def extract(pdf, ctx):
        year = "2025" if "2025" in pdf.name else "2026"
        return ex.model_copy(update={"doc": pdf.name, "agm_date": f"{year}-06-18"})

    def texts(pdf):
        if pdf.name in broken:
            raise RuntimeError("cannot read page tree")
        return [p.replace("2026", "2025") for p in pages] if "2025" in pdf.name else pages

    monkeypatch.setattr(validate, "extract_notice", extract)
    monkeypatch.setattr(validate, "page_texts", texts)


def test_two_notices_from_one_company_get_distinct_keys(ctx, tmp_path, monkeypatch):
    ex, pages = tesco(ctx)
    for year in (2025, 2026):
        (tmp_path / f"Tesco_{year}_Notice_of_AGM.pdf").write_bytes(b"")
    _fake_notices(monkeypatch, ex, pages)
    report = validate.run(ctx, notices_dir=tmp_path)
    assert report.ok, report.errors
    keys = {m.key for m in db.get_outputs(ctx.conn, ctx.run_id, "meeting")}
    assert keys == set(notices(tmp_path)) == {"tesco-2025-notice-of-agm",
                                              "tesco-2026-notice-of-agm"}


def test_an_exception_in_one_notice_fails_only_that_notice(ctx, tmp_path, monkeypatch):
    ex, pages = tesco(ctx)
    for year in (2025, 2026):
        (tmp_path / f"Tesco_{year}_Notice_of_AGM.pdf").write_bytes(b"")
    _fake_notices(monkeypatch, ex, pages, broken={"Tesco_2025_Notice_of_AGM.pdf"})
    report = validate.run(ctx, notices_dir=tmp_path)
    assert not report.ok
    [error] = report.errors
    assert "Tesco_2025" in error and "RuntimeError: cannot read page tree" in error
    meetings = db.get_outputs(ctx.conn, ctx.run_id, "meeting")
    assert [m.key for m in meetings] == ["tesco-2026-notice-of-agm"]
    reviews = db.get_outputs(ctx.conn, ctx.run_id, "review")
    assert any(r.kind == "extract_error" and "Tesco_2025" in r.detail for r in reviews)
