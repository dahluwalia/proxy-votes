"""M5.1: one test per role-table row, the H7 cases, tense, and listed_by_rule."""

from __future__ import annotations

import pytest

from pennant.classify.roles import classify_role, listed_by_rule, role_type_of
from pennant.core.models import RoleType as R

# (role text, role_type, board, chair, exec): one or more examples per table row.
TABLE = [
    ("Executive Chair", R.executive_chair, "yes", "yes", "yes"),
    ("Interim executive chair", R.executive_chair, "yes", "yes", "yes"),  # Gilliland, IG Design
    ("Executive Chairman", R.executive_chair, "yes", "yes", "yes"),
    ("Chair", R.chair, "yes", "yes", "no"),
    ("Non-executive Chairman", R.chair, "yes", "yes", "no"),
    ("Deputy Chair", R.deputy_chair, "yes", "no", "no"),
    ("Vice-Chairman", R.deputy_chair, "yes", "no", "no"),
    ("Non-executive director", R.non_executive_director, "yes", "no", "no"),
    ("Independent Non‑Executive Director", R.non_executive_director, "yes", "no", "no"),
    ("Senior independent director", R.non_executive_director, "yes", "no", "no"),
    ("Lead Director", R.non_executive_director, "yes", "no", "no"),
    ("Executive Director", R.executive_director, "yes", "no", "yes"),
    ("Director", R.director_unspecified, "yes", "no", "unknown"),
    ("Chief executive officer", R.officer_title, "unknown", "no", "yes"),
    ("CFO", R.officer_title, "unknown", "no", "yes"),
    ("Chief financial officer and chief operating officer", R.officer_title,
     "unknown", "no", "yes"),
    ("President", R.officer_title, "unknown", "no", "yes"),
    ("EVP", R.officer_title, "unknown", "no", "yes"),
    ("Managing Director", R.officer_title, "unknown", "no", "yes"),
    ("Partner", R.officer_title, "unknown", "no", "yes"),
    ("Chair of the Audit and Risk Committee", R.committee_chair, "no", "no", "no"),
    ("Audit Committee Chair", R.committee_chair, "no", "no", "no"),
    ("Senior Adviser", R.adviser, "no", "no", "no"),
    ("Member of the Advisory Board", R.adviser, "no", "no", "no"),
    ("Advisory council member", R.adviser, "no", "no", "no"),  # Frontier Economics
    ("Mentor", R.adviser, "no", "no", "no"),
    ("Trustee", R.trustee, "no", "no", "no"),
    ("Chair of the Almeida Theatre Board of Trustees", R.trustee, "no", "no", "no"),
    ("Honorary fellow", R.trustee, "no", "no", "no"),
    ("Founder", R.other, "unknown", "unknown", "unknown"),
]


@pytest.mark.parametrize(("text", "role_type", "board", "chair", "exe"), TABLE)
def test_table_rows(text: str, role_type: R, board: str, chair: str, exe: str) -> None:
    f = classify_role(text, "current")
    assert (f.role_type, f.board_seat, f.chair, f.executive) == (role_type, board, chair, exe)


# H7: a role is not a seat. Wording as printed in the notices.
H7 = [
    ("Executive Vice President, Global Planning, Engineering and Operations",  # Preet, FedEx
     R.officer_title, "unknown"),
    ("Chief Financial Officer", R.officer_title, "unknown"),  # Dawn Allen, Haleon (prose only)
    ("Chief executive officer", R.officer_title, "unknown"),  # Bodson, Keywords Studios Ltd
    ("Senior advisor", R.adviser, "no"),  # Gerry Murphy, Perella Weinberg; Bethell, Atairos
    ("International Advisory Board", R.adviser, "no"),  # Caroline Silver, Adobe
    ("Committee Chair", R.committee_chair, "no"),
    ("Chair of the Audit Committee", R.committee_chair, "no"),
]


@pytest.mark.parametrize(("text", "role_type", "board"), H7)
def test_h7_role_is_not_a_seat(text: str, role_type: R, board: str) -> None:
    f = classify_role(text, "current")
    assert (f.role_type, f.board_seat) == (role_type, board)
    assert f.board_seat != "yes"


@pytest.mark.parametrize(("text", "role_type"), [
    ("Chair and Chief Executive Officer", R.executive_chair),
    ("Managing Director and Chairman", R.executive_chair),  # Greenhill & Co.
    ("Director and Chief Financial Officer", R.executive_director),
    ("Non-executive Director and Chair of the Audit Committee", R.non_executive_director),
    ("Senior Independent Director and Remuneration Committee Chair", R.non_executive_director),
    ("Chair of the Board and their Nomination Committee", R.chair),
    ("Deputy Chair and Senior Independent Director", R.deputy_chair),
    ("Non-executive Director and Partner", R.non_executive_director),
])
def test_compound_titles(text: str, role_type: R) -> None:
    assert role_type_of(text)[0] == role_type


@pytest.mark.parametrize(("tense", "current"), [
    ("current", "yes"), ("past", "no"), ("future", "unknown"), ("unclear", "unknown"),
    (None, "unknown"), ("Current ", "yes"),
])
def test_tense(tense: str | None, current: str) -> None:
    assert classify_role("Chair", tense).current == current


def test_past_cue_in_text_conflicts_with_current_tense() -> None:
    assert classify_role("Former Chief Executive", "current").current == "unknown"
    assert classify_role("Former Chief Executive", "past").current == "no"


def test_chair_designate_follows_policy_flag() -> None:
    assert classify_role("Chair designate").chair == "unknown"
    assert classify_role("Chair-designate", chair_designate="yes").chair == "yes"
    assert classify_role("Chair designate").board_seat == "yes"


def test_missing_role_is_other_and_unknown() -> None:
    f = classify_role(None, "current")
    assert f.role_type == R.other
    assert (f.board_seat, f.chair, f.executive) == ("unknown",) * 3


@pytest.mark.parametrize(("ch_type", "listed"), [
    ("ltd", "no"),
    ("llp", "no"),  # Greenhill & Co. International LLP
    ("private-limited-guarant-nsc", "no"),
    ("private-unlimited", "no"),
    ("charitable-incorporated-organisation", "no"),
    ("plc", "unknown"),
    ("oversea-company", "unknown"),
    ("royal-charter", "unknown"),
    (None, "unknown"),
    ("", "unknown"),
])
def test_listed_by_rule(ch_type: str | None, listed: str) -> None:
    assert listed_by_rule(ch_type) == listed


def test_listed_never_from_name_suffix() -> None:
    # Capri Holdings Limited is NYSE-listed; Keywords Studios Ltd is private. Only the CH
    # company type can rule `no`, and a name is not a company type.
    assert listed_by_rule("Capri Holdings Limited") == "unknown"
    assert listed_by_rule("Keywords Studios Ltd") == "unknown"


@pytest.mark.parametrize(("text", "role_type"), [
    # Mogford at Costain (Intertek r12): a committee seat is not a board seat by itself.
    ("member of the Audit, Nomination and Remuneration Committees", R.committee_chair),
    ("Member of the Remuneration Committee", R.committee_chair),
    ("Audit Committee member", R.committee_chair),
    ("Non-executive Director and member of the Audit Committee", R.non_executive_director),
    ("Member of the Board", R.director_unspecified),
    # An executive committee is an executive job, not a board committee.
    ("Member of the Executive Committee", R.officer_title),
    ("Group Executive Committee member", R.officer_title),
])
def test_committee_members(text: str, role_type: R) -> None:
    f = classify_role(text, "current")
    assert f.role_type == role_type
    if role_type == R.committee_chair:
        assert (f.board_seat, f.chair, f.executive) == ("no", "no", "no")


# --- review fixes -------------------------------------------------------------------

@pytest.mark.parametrize(("text", "role_type", "board", "chair"), [
    # comma / colon committee-chair forms are committee chairs, not a 2-point board chair
    ("Chair, Audit Committee", R.committee_chair, "no", "no"),
    ("Chair: Audit and Risk Committee", R.committee_chair, "no", "no"),
    ("Chairman, Nomination Committee", R.committee_chair, "no", "no"),
    ("Senior Independent Director and Chair, Remuneration Committee",
     R.non_executive_director, "yes", "no"),
    ("Chair, Board of Trustees", R.trustee, "no", "no"),  # Ingram, Almeida Theatre wording
    ("Chair, Advisory Board", R.adviser, "no", "no"),
    # ... while a real chair next to a committee role keeps its chair
    ("Chair and member of the Audit Committee", R.chair, "yes", "yes"),
    ("Chair and Audit Committee member", R.chair, "yes", "yes"),
    ("Chair of XYZ plc and member of its Audit Committee", R.chair, "yes", "yes"),
    ("Chair and Trustee", R.chair, "yes", "yes"),
])
def test_comma_and_colon_committee_chairs(text, role_type, board, chair) -> None:
    f = classify_role(text, "current")
    assert (f.role_type, f.board_seat, f.chair) == (role_type, board, chair)


@pytest.mark.parametrize(("text", "role_type", "board", "exe"), [
    ("Director", R.director_unspecified, "yes", "unknown"),
    ("Board Director", R.director_unspecified, "yes", "unknown"),
    ("Director of the Board", R.director_unspecified, "yes", "unknown"),
    ("Trustee and Director", R.director_unspecified, "yes", "unknown"),
    ("Director of Strategy", R.officer_title, "unknown", "yes"),
    ("Director, Corporate Affairs", R.officer_title, "unknown", "yes"),
    ("Regional Director", R.officer_title, "unknown", "yes"),
    ("Director (non-board)", R.director_unspecified, "no", "unknown"),
    ("Non-board Director", R.director_unspecified, "no", "unknown"),
])
def test_only_a_bare_director_is_a_board_seat(text, role_type, board, exe) -> None:
    f = classify_role(text, "current")
    assert (f.role_type, f.board_seat, f.executive) == (role_type, board, exe)


@pytest.mark.parametrize("text", [
    "Chair of the Executive Committee", "Chair of the Group Executive Committee",
    "Chair, Group Executive Committee", "Executive Committee Chair",
    "Chairman of the Management Committee"])
def test_chairing_an_executive_committee_is_an_executive_job(text) -> None:
    f = classify_role(text, "current")
    assert (f.role_type, f.board_seat, f.chair, f.executive) == (
        R.officer_title, "unknown", "no", "yes")


@pytest.mark.parametrize(("text", "later"), [
    ("Chair with effect from 2023", False),  # a sitting chair
    ("Chair, effective 3 March 2025", False),
    ("Chair with effect from 1 June 2026", True),
    ("Chair with effect from May 2026", True),  # may fall after the AGM: keep the flag
    ("Chair with effect from 1/6/2026", True),  # unreadable date: keep the flag
    ("Chair from the conclusion of the 2026 AGM", True),
    ("Chair designate", True),
    ("Will become Chair", True),
    ("Chair", False),
])
def test_starts_after(text, later) -> None:
    from datetime import date

    from pennant.classify.roles import starts_after
    assert starts_after(text, date(2026, 5, 7)) is later
