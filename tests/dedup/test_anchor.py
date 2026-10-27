"""M4.2: anchor notice names to issuer officers, then expand by date of birth.

The first half uses hand-built officers and a patched client (no fixtures). The
second half runs on the recorded CH fixtures and skips until they exist.
"""

from __future__ import annotations

from datetime import date

import pytest

from pennant.ch import client
from pennant.ch.client import CHResult
from pennant.core import db
from pennant.core.config import FIXTURES_DIR
from pennant.core.models import CHAppointment, CHOfficer, Meeting, RosterEntry, meeting_id
from pennant.dedup import anchor as m42

TESCO, AGM = "00445790", date(2026, 6, 18)
MTG = meeting_id(TESCO, AGM)


def officer(oid, surname, forenames, dob=(6, 1960), role="director",
            appointed=date(2020, 1, 1), resigned=None, number=TESCO) -> CHOfficer:
    appt = CHAppointment(company_number=number, company_name="TESCO PLC", role=role,
                         appointed_on=appointed, resigned_on=resigned)
    return CHOfficer(officer_id=oid, name=f"{surname}, {forenames}", surname=surname,
                     forenames=forenames, dob_month=dob[0], dob_year=dob[1],
                     appointments=[appt])


def entry(name, res=4) -> RosterEntry:
    return RosterEntry(mention_id=f"nm:{TESCO}:r{res}", meeting_id=MTG, resolution_no=res,
                       name=name, coverage="structured")


BOARD = [
    officer("ken", "MURPHY", "Kenneth", dob=(9, 1967)),
    officer("gerry", "MURPHY", "Gerard Martin", dob=(1, 1955)),
    officer("ck", "KENNEDY", "Christopher Michael", dob=(3, 1964)),
    officer("old", "SILVER", "Caroline", resigned=date(2024, 1, 1)),
    officer("sec", "SECRETARY", "Christopher", role="secretary"),
]


# --- anchor ------------------------------------------------------------------


def test_h1_ken_and_gerry_murphy_anchor_to_different_officers():
    ken, _ = m42.anchor(entry("Ken Murphy"), BOARD)
    gerry, _ = m42.anchor(entry("Dr Gerry Murphy"), BOARD)
    assert (ken.officer_id, gerry.officer_id) == ("ken", "gerry")


def test_variant_anchor_with_titles_and_honours():
    found, reviews = m42.anchor(entry("Chris Kennedy CBE"), BOARD)
    assert found.officer_id == "ck" and reviews == []


def test_former_director_anchors_only_with_a_review_item():
    """No one in office matches, but a director who left before the AGM clearly does: the
    record anchors, so reconcile can mark the notice's seat stale from its dates."""
    found, [review] = m42.anchor(entry("Caroline Silver"), BOARD)
    assert found.officer_id == "old" and review.kind == "not_in_office"


def test_secretaries_are_not_anchors():
    found, [review] = m42.anchor(entry("Chris Secretary"), BOARD)
    assert found is None and review.kind == "unanchored"


def test_two_good_matches_are_a_review_item_not_a_guess():
    board = [officer("c1", "SMITH", "Christopher"), officer("c2", "SMITH", "Christine")]
    found, [review] = m42.anchor(entry("Chris Smith"), board)
    assert found is None
    assert review.kind == "possible_same_person" and "c1" in review.detail and "c2" in review.detail


def test_no_match_is_unanchored_with_the_closest_candidates():
    found, [review] = m42.anchor(entry("Karen Murphy"), BOARD)
    assert found is None and review.kind == "unanchored"
    assert review.subject == f"nm:{TESCO}:r4"
    assert review.suggested_command.startswith("pennant merge")


def test_not_in_office_detail_gives_the_directorship_dates():
    board = BOARD + [officer("tg", "GARNIER", "Thierry Dominique Gerard",
                             appointed=date(2021, 4, 30), resigned=date(2026, 5, 6))]
    found, [review] = m42.anchor(entry("Thierry Garnier"), board)
    assert found.officer_id == "tg" and review.kind == "not_in_office"
    assert "a director of 00445790 2021-04-30 to 2026-05-06 but not on the AGM date" \
        in review.detail


def test_anchor_evidence_cites_the_issuer_officer_list():
    [ev] = m42.anchor_evidence(entry("Ken Murphy"), BOARD[0])
    assert ev.source == "ch"
    assert ev.ref == f"{client.WEB_URL}/company/{TESCO}/officers"
    assert "MURPHY, Kenneth" in ev.quote and "09/1967" in ev.quote and "2020-01-01" in ev.quote


# --- expand (client patched) -------------------------------------------------


@pytest.fixture
def patched(monkeypatch):
    calls = {"search": [], "appointments": []}

    def install(search: CHResult, appointments=None):
        def fake_search(name, ctx):
            calls["search"].append(name)
            return search

        def fake_appointments(oid, ctx, types_since=None):
            calls["appointments"].append(oid)
            return (appointments or {}).get(oid, CHResult("unavailable", error="boom"))

        monkeypatch.setattr(client, "search_officers", fake_search)
        monkeypatch.setattr(client, "appointments", fake_appointments)
        return calls

    return install


def hit(oid, surname, forenames, dob):
    return CHOfficer(officer_id=oid, name=f"{forenames} {surname}", surname=surname,
                     forenames=forenames, dob_month=dob[0], dob_year=dob[1])


def test_h5_only_same_dob_and_exact_name_links(patched, ctx):
    ck = BOARD[2]
    hits = [
        ck,  # the anchor itself comes back from search
        hit("ck2", "KENNEDY", "Christopher Michael", (3, 1964)),  # same person: link
        hit("ck3", "KENNEDY", "Christopher", (7, 1971)),  # another Christopher Kennedy
        hit("ck4", "KENNEDY", "Chris", (3, 1964)),  # same DOB, variant only: review
        hit("ck5", "KENNEDY", "Christopher John", (3, 1964)),  # same DOB, conflict: ignored
    ]
    full = officer("ck2", "KENNEDY", "Christopher Michael", dob=(3, 1964), number="01234567")
    calls = patched(CHResult("ok", hits), {"ck2": CHResult("ok", full)})
    found, reviews = m42.expand(ck, ctx)
    assert calls["search"] == ["Christopher Michael KENNEDY"]
    assert [(o.officer_id, o.appointments[0].company_number) for o, _ in found] == \
        [("ck2", "01234567")]
    [ev] = found[0][1]
    assert ev.ref.endswith("/officers/ck2/appointments") and "03/1964" in ev.quote
    assert [(r.kind, r.subject) for r in reviews] == [("possible_same_person", "ck")]
    assert "ck4" in reviews[0].suggested_command


def test_dob_link_allows_a_missing_middle_name_but_not_a_different_one(patched, ctx):
    """CH often files one person with and without middle names: those link on a shared
    DOB. A contradicting middle name is a conflict and is ignored."""
    ck = BOARD[2]  # KENNEDY, Christopher Michael
    hits = [hit("ck6", "KENNEDY", "Christopher", (3, 1964)),
            hit("ck7", "KENNEDY", "Christopher M", (3, 1964)),
            hit("ck8", "KENNEDY", "Christopher Paul", (3, 1964))]
    patched(CHResult("ok", hits))
    found, reviews = m42.expand(ck, ctx)
    assert sorted(o.officer_id for o, _ in found) == ["ck6", "ck7"]
    assert not [r for r in reviews if r.kind == "possible_same_person"]


def test_search_with_unread_hits_is_not_a_review_item(patched, ctx):
    patched(CHResult("ok", [], unread=9800))
    assert m42.expand(BOARD[2], ctx) == ([], [])


def test_search_cut_off_amid_namesakes_is_a_review_item(patched, ctx):
    patched(CHResult("ok", [], unread=9000, complete=False))
    found, [review] = m42.expand(BOARD[2], ctx)
    assert found == [] and review.kind == "search_incomplete" and "9000 unread" in review.detail


def test_failed_search_fails_closed(patched, ctx):
    patched(CHResult("unavailable", error="HTTP 503 after 5 tries"))
    found, [review] = m42.expand(BOARD[2], ctx)
    assert found == [] and review.kind == "ch_unavailable" and "503" in review.detail


def test_linked_record_whose_appointments_fail_is_kept_and_flagged(patched, ctx):
    patched(CHResult("ok", [hit("ck2", "KENNEDY", "Christopher Michael", (3, 1964))]))
    found, [review] = m42.expand(BOARD[2], ctx)
    assert [o.officer_id for o, _ in found] == ["ck2"]
    assert review.kind == "ch_unavailable" and review.subject == "ck2"


def test_no_dob_means_no_expansion(patched, ctx):
    calls = patched(CHResult("ok", []))
    no_dob = BOARD[0].model_copy(update={"dob_month": None, "dob_year": None})
    found, [review] = m42.expand(no_dob, ctx)
    assert found == [] and review.kind == "no_dob" and calls["search"] == []


# --- on the recorded CH fixtures ---------------------------------------------

# (company, number, AGM date, first resolution, names in resolution order): the 50 mentions
NOTICES = [
    ("ITV plc", "04967001", date(2026, 5, 7), 4, [
        "Dawn Allen", "Helen Ashton", "Edward Bonham Carter", "Graham Cooke",
        "Andrew Cosslett", "Margaret Ewing", "Marjorie Kaplan", "Gidon Katz", "Chris Kennedy",
        "Carolyn McCall", "Sharmila Nebhrajani"]),
    ("Intertek Group plc", "04267576", date(2026, 5, 20), 5, [
        "Laura Crespi", "André Lacroix", "Graham Allan", "Hilde Merete Aasheim",
        "Robin Freestone", "Tamara Ingram", "Jez Maiden", "Steve Mogford", "Kawal Preet",
        "Apurvi Sheth", "Jean-Michel Valette"]),
    ("Tesco PLC", "00445790", date(2026, 6, 18), 4, [
        "Melissa Bethell", "Bertrand Bodson", "Dame Carolyn Fairbairn", "Thierry Garnier",
        "Stewart Gilliland", "Chris Kennedy", "Dr Gerry Murphy", "Ken Murphy", "Imran Nawaz",
        "Caroline Silver", "Karen Whitworth"]),
    ("Marks and Spencer Group plc", "04256886", date(2026, 7, 7), 5, [
        "Archie Norman", "Stuart Machin", "Alison Dolan", "Fiona Dawson", "Evelyn Bourke",
        "Tamara Ingram", "Sapna Sood", "Roger Burnley", "Sean Doyle"]),
    ("Severn Trent Plc", "02366619", date(2026, 7, 9), 5, [
        "Christine Hodgson CBE", "James Jesic", "Helen Miles", "Nick Hampton", "Tom Delay CBE",
        "Sarah Legg", "Sharmila Nebhrajani OBE", "Richard Taylor"]),
]
# Roster entries the CH fixtures can't anchor, and why. Each one becomes a review item.
EXCEPTIONS: dict[str, str] = {
    # "Tom" shares only its initial with CH's "Thomas" (score 0.19): the analyst confirms.
    "Tom Delay CBE": "Thomas Auguste Read DELAY",
}

needs_fixtures = pytest.mark.skipif(
    not any((FIXTURES_DIR / "ch").glob("*.json")), reason="CH fixtures not recorded yet")


@pytest.fixture
def recorded(ctx):
    """The fixture cache, the CH stage run offline, and every roster entry's anchor."""
    client.load_fixtures(ctx.conn)
    meetings, roster = [], []
    for company, number, agm, first, names in NOTICES:
        mid = meeting_id(number, agm)
        meetings.append(Meeting(meeting_id=mid, company_name=company, company_number=number,
                                agm_date=agm, doc_sha="fixture"))
        roster += [RosterEntry(mention_id=f"nm:{number}:r{first + i}", meeting_id=mid,
                               resolution_no=first + i, name=n, coverage="structured")
                   for i, n in enumerate(names)]
    db.put_outputs(ctx.conn, ctx.run_id, "meeting", meetings)
    assert client.run(ctx).ok
    officers = db.get_outputs(ctx.conn, ctx.run_id, "officer")
    anchors = {e.name + "@" + e.meeting_id: m42.anchor(e, officers) for e in roster}
    return ctx, roster, officers, anchors


@needs_fixtures
def test_every_roster_entry_anchors_or_is_a_known_exception(recorded):
    _, roster, _, anchors = recorded
    assert len(roster) == 50
    missing = {key.split("@")[0]: reviews[0].detail
               for key, (found, reviews) in anchors.items() if found is None}
    assert set(missing) == set(EXCEPTIONS), missing
    for name, detail in missing.items():
        assert EXCEPTIONS[name] in detail, detail


@needs_fixtures
def test_short_form_of_a_middle_name_anchors(recorded):
    _, _, _, anchors = recorded
    hampton, _ = anchors[f"Nick Hampton@{meeting_id('02366619', date(2026, 7, 9))}"]
    assert hampton and hampton.forenames == "Anthony Nicholas Seymour"


@needs_fixtures
def test_h1_on_fixtures_ken_and_gerry_murphy_are_different_officers(recorded):
    _, _, _, anchors = recorded
    tesco = meeting_id(TESCO, AGM)
    ken, _ = anchors[f"Ken Murphy@{tesco}"]
    gerry, _ = anchors[f"Dr Gerry Murphy@{tesco}"]
    assert ken and gerry and ken.officer_id != gerry.officer_id
    assert (ken.dob_month, ken.dob_year) != (gerry.dob_month, gerry.dob_year)


@needs_fixtures
def test_h5_on_fixtures_no_other_christopher_kennedy_is_linked(recorded):
    """The negative case: many Christopher John Kennedys come back, none with his DOB."""
    ctx, _, _, anchors = recorded
    kennedy, _ = anchors[f"Chris Kennedy@{meeting_id(TESCO, AGM)}"]
    found, _ = m42.expand(kennedy, ctx)
    assert found == []
    dob = (kennedy.dob_month, kennedy.dob_year)
    search = client.search_officers(m42.natural_name(kennedy), ctx)
    others = [h for h in search.data if h.surname == "KENNEDY" and (h.dob_month, h.dob_year) != dob]
    assert others, "the search should also return Christopher Kennedys with other DOBs"


ITV_MTG = meeting_id("04967001", date(2026, 5, 7))


@needs_fixtures
def test_on_fixtures_a_same_dob_same_name_record_links(recorded):
    """The positive case: Tamara Ingram's other CH records share her DOB and name."""
    ctx, _, _, anchors = recorded
    ingram, _ = anchors[f"Tamara Ingram@{meeting_id('04256886', date(2026, 7, 7))}"]
    found, _ = m42.expand(ingram, ctx)
    assert found
    for o, [ev] in found:
        assert (o.dob_month, o.dob_year) == (ingram.dob_month, ingram.dob_year)
        assert o.surname == "INGRAM" and o.officer_id in ev.ref


@needs_fixtures
def test_on_fixtures_an_honour_in_a_search_title_does_not_drop_the_record(recorded):
    """'Sharmila NEBHRAJANI  OBE' and 'Carolyn MCCALL DBE' (no 'Julia') both link on
    their DOB: the honour no longer hides them."""
    ctx, _, _, anchors = recorded
    nebhrajani, _ = anchors[f"Sharmila Nebhrajani@{ITV_MTG}"]
    found, _ = m42.expand(nebhrajani, ctx)
    assert "-A0-iiopZsWl7Bz6c5gP0sKHOk8" in {o.officer_id for o, _ in found}
    mccall, _ = anchors[f"Carolyn McCall@{ITV_MTG}"]
    found, reviews = m42.expand(mccall, ctx)
    assert "7NK8UpcMxJj8JuQLvlgw0pS-oV4" in {o.officer_id for o, _ in found}
    assert not [r for r in reviews if r.kind == "possible_same_person"]


# --- the TF-IDF ranker (no name dictionary) ----------------------------------


def test_ranker_separates_same_surname_directors_by_first_name():
    ranked = m42.rank("Dr Gerry Murphy", BOARD)
    assert [o.officer_id for _, o in ranked] == ["gerry", "ken"]
    (top, _), (second, _) = ranked
    assert top >= m42.MIN_SCORE and top - second >= m42.MARGIN
    assert m42.rank("Ken Murphy", BOARD)[0][1].officer_id == "ken"


def test_ranker_scores_a_middle_name_and_ignores_other_surnames():
    board = [officer("h", "HAMPTON", "Anthony Nicholas Seymour"), officer("x", "SILVER", "Nick")]
    [(score, o)] = m42.rank("Nick Hampton", board)
    assert o.officer_id == "h" and score >= m42.MIN_SCORE


def test_initial_only_overlap_is_a_review_item_not_an_anchor():
    """Tom/Thomas share only the first letter: below MIN_SCORE, so the analyst decides."""
    board = [officer("td", "DELAY", "Thomas Auguste Read")]
    found, [review] = m42.anchor(entry("Tom Delay CBE"), board)
    assert found is None and review.kind == "unanchored" and "DELAY, Thomas" in review.detail


def test_names_with_no_shared_letters_never_anchor():
    found, [review] = m42.anchor(entry("Bob Smith"), [officer("r", "SMITH", "Robert")])
    assert found is None and review.kind == "unanchored"



def test_fetch_officer_loads_a_merged_record_or_fails_closed(recorded):
    """A recorded record comes back whole; an unrecorded one is ch_unavailable, not empty."""
    ctx = recorded[0]
    record, items = m42.fetch_officer("7NK8UpcMxJj8JuQLvlgw0pS-oV4", ctx)  # Carolyn McCall
    assert record and record.appointments and items == []
    record, items = m42.fetch_officer("not-a-recorded-officer-id", ctx)
    assert record is None and [i.kind for i in items] == ["ch_unavailable"]
