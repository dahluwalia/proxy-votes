"""M6.1 score: the hard-case oracles (H6, H7, H11–H14, H17), policy flags and overrides."""

from __future__ import annotations

from datetime import datetime

import pytest

from pennant.core.config import load_policy
from pennant.core.models import Decision, RoleType, Vote
from pennant.policy import score
from pennant.state.decisions import apply_set_facts
from tests.policy.seat_cases import CASES, load_case, score_case, with_policy


def set_fact(target: str, field: str, value: str, kind: str = "seat", n: int = 1) -> Decision:
    return Decision(decision_id=f"dec:{n}", kind="set_fact", author="expert", reason="checked",
                    payload={"kind": kind, "target": target, "field": field, "value": value},
                    created_at=datetime(2026, 10, 26))


@pytest.mark.parametrize("stem", CASES)
def test_case_oracle(stem):
    case = load_case(stem)
    scored = score_case(case)
    assert set(scored) == set(case.expect)
    for key, exp in case.expect.items():
        rec = scored[key].rec
        assert rec.vote == exp["vote"], (key, rec.reasons)
        assert [rec.points_lo, rec.points_hi] == exp["points"], (key, rec.reasons)
        for fragment in exp.get("blocked_on", []):
            assert any(fragment in f for f in rec.unknown_facts), rec.unknown_facts
        if rec.vote != Vote.REVIEW:
            assert rec.unknown_facts == []  # a decided vote can't flip


def test_h12_hodgson_standard_chartered_sensitivity():
    """At exactly 4, one past seat misread as current turns FOR into AGAINST. CH guards it:
    it shows her resignation, so the seat goes stale and the vote waits for an analyst."""
    case = load_case("christine_hodgson")
    sc = case.seat("standard-chartered")
    case.seats[case.seats.index(sc)] = sc.model_copy(update={"current": "yes"})
    notice_only = score_case(case, ch=None)["severn-trent"].rec
    assert (notice_only.vote, notice_only.points_lo, notice_only.points_hi) == (
        Vote.AGAINST, 5, 7)  # AGAINST whatever her unread CH record holds (0–2 more)
    with_ch = score_case(case)["severn-trent"]
    assert (with_ch.rec.vote, with_ch.rec.points_lo, with_ch.rec.points_hi) == (Vote.REVIEW, 4, 5)
    assert with_ch.seat("standard-chartered").status == "stale"
    assert any(i.kind == "stale" for i in with_ch.items)


def test_silver_moelis_set_fact_moves_her_to_for_and_cites_the_decision():
    case = load_case("caroline_silver")
    moelis = case.seat("moelis").seat_id
    dec = set_fact(moelis, "board_seat", "no", n=3)
    scored = score_case(case, decisions=(dec,))["tesco"]
    assert (scored.rec.vote, scored.rec.points_lo, scored.rec.points_hi) == (Vote.FOR, 4, 4)
    assert any("dec:3" in r for r in scored.rec.reasons)
    assert any(e.source == "analyst" and e.ref == "dec:3" for e in scored.rec.evidence)
    # the seat written back for gate G4 carries the analyst's value
    out = {s.seat_id: s for s in apply_set_facts(scored.seats, "seat", [dec])}
    assert out[moelis].board_seat == "no"


@pytest.mark.parametrize("flag,vote", [("yes", Vote.AGAINST), ("no", Vote.FOR),
                                       ("unknown", Vote.REVIEW)])
def test_h13_gilliland_follows_the_aim_flag(flag, vote):
    rec = score_case(load_case("stewart_gilliland"), with_policy(aim_counts_as_listed=flag))
    assert rec["tesco"].rec.vote == vote
    assert any("AIM" in r for r in rec["tesco"].rec.reasons)


def test_analyst_listed_override_beats_the_aim_flag():
    case = load_case("stewart_gilliland")
    dec = set_fact(case.seat("ig-design").seat_id, "listed", "no")
    rec = score_case(case, with_policy(aim_counts_as_listed="yes"), (dec,))["tesco"].rec
    assert rec.vote == Vote.FOR


def test_org_override_changes_every_seat_at_that_org():
    dec = set_fact("ig-design-group", "market", "LSE Main Market", kind="org")
    rec = score_case(load_case("stewart_gilliland"), decisions=(dec,))["tesco"].rec
    assert rec.vote == Vote.AGAINST and any("dec:1" in r for r in rec.reasons)


@pytest.mark.parametrize("flag,points", [("yes", (4, 4)), ("no", (3, 3)), ("unknown", (3, 4))])
def test_h13_mogford_chair_designate_is_for_at_3_or_4(flag, points):
    rec = score_case(load_case("steve_mogford"),
                     with_policy(chair_designate_counts_as_chair=flag))["intertek"].rec
    assert (rec.vote, (rec.points_lo, rec.points_hi)) == (Vote.FOR, points)


def test_h17_group_dedupe_collapses_the_lloyds_boards():
    """With debt-only listings counting, Lloyds Bank and Bank of Scotland are listed seats
    (NED, from Lloyds' own site); in one group they count once, apart they push Sarah Legg
    over the threshold."""
    case = load_case("sarah_legg")
    together = score_case(case, with_policy(debt_only_listing_counts="yes"))["severn-trent"].rec
    apart = score_case(case, with_policy(debt_only_listing_counts="yes",
                                         group_dedupe="no"))["severn-trent"].rec
    either = score_case(case, with_policy(debt_only_listing_counts="yes",
                                          group_dedupe="unknown"))["severn-trent"].rec
    assert (together.vote, together.points_lo, together.points_hi) == (Vote.FOR, 3, 3)
    assert (apart.vote, apart.points_lo, apart.points_hi) == (Vote.AGAINST, 5, 5)
    assert (either.vote, either.points_lo, either.points_hi) == (Vote.REVIEW, 3, 5)


def test_h17_legg_undisclosed_lloyds_seats_are_flagged():
    scored = score_case(load_case("sarah_legg"))["severn-trent"]
    undisclosed = sorted(s.org_name for s in scored.seats if s.status == "undisclosed")
    assert undisclosed == ["BANK OF SCOTLAND PLC", "LLOYDS BANK PLC"]
    assert scored.seat("man-group").status == "ch_invisible"  # not on CH, still listed
    assert scored.seat("hafren").status == "confirmed"


def test_h7_preet_unsure_research_is_review_never_against():
    case = load_case("kawal_preet")
    fedex = case.seats.index(next(s for s in case.seats if s.role_type == "officer_title"
                                  and s.current == "yes"))
    case.seats[fedex] = case.seats[fedex].model_copy(update={"board_seat": "unknown"})
    rec = score_case(case)["intertek"].rec
    assert rec.vote == Vote.REVIEW
    assert rec.unknown_facts == [f"{case.seats[fedex].seat_id}.board_seat"]


def test_h16_ch_unavailable_never_turns_into_for():
    rec = score_case(load_case("thierry_garnier"), ch=None)["tesco"].rec
    assert rec.vote == Vote.REVIEW
    assert any("ch_unavailable" in r for r in rec.reasons)


def test_h11_ms_unknown_role_blocks_only_when_points_could_pass_the_threshold():
    case = load_case("fiona_dawson")
    extra = [case.seats[0].model_copy(update={
        "seat_id": f"seat:x:{n}", "org_name": f"Listed {n} plc", "org_key": f"listed-{n}",
        "board_seat": "yes", "chair": "no", "executive": "no", "listed": "yes"})
        for n in range(4)]
    case.seats += extra
    rec = score_case(case)["ms"].rec
    mention = case.roster[0].mention_id
    assert (rec.vote, rec.points_lo, rec.points_hi) == (Vote.REVIEW, 5, 6)
    assert f"{mention}.role_type_at_issuer" in rec.unknown_facts
    for kind, target, field in [("roster", mention, "role_type_at_issuer"),
                                ("seat", case.seats[0].seat_id, "role_type")]:
        dec = set_fact(target, field, "non_executive_director", kind=kind, n=7)
        as_ned = score_case(case, decisions=(dec,))["ms"].rec
        assert as_ned.vote == Vote.AGAINST, kind
        assert any(e.source == "analyst" and e.ref == "dec:7" for e in as_ned.evidence)


def test_ingram_ms_role_comes_from_her_intertek_biography():
    rec = score_case(load_case("tamara_ingram"))["ms"].rec
    assert rec.role_at_meeting == "non_executive_director"


def test_count_seat_under_vote_and_threshold_flags():
    case = load_case("gerry_murphy")
    excl = score_case(case, with_policy(count_seat_under_vote="no"))["tesco"].rec
    assert (excl.points_lo, excl.points_hi) == (2, 2)
    strict = score_case(case, with_policy(ned_points_threshold=3))["tesco"].rec
    assert strict.vote == Vote.AGAINST


def test_reasons_and_evidence_explain_the_vote():
    rec = score_case(load_case("thierry_garnier"))["tesco"].rec
    assert any(r.startswith("Kingfisher plc") and "executive" in r for r in rec.reasons)
    assert "executive rule: yes" in rec.reasons
    assert any(e.source == "ch" and "KINGFISHER" in (e.quote or "") for e in rec.evidence)


def test_org_listed_override_also_sets_equity_listing():
    """E3: an analyst's org `listed` means listed for the policy, as classify applies it."""
    dec = set_fact("moelis-and-company", "listed", "no", kind="org", n=4)
    rec = score_case(load_case("caroline_silver"), decisions=(dec,))["tesco"].rec
    assert (rec.vote, rec.points_lo, rec.points_hi) == (Vote.FOR, 4, 4)
    assert any(e.ref == "dec:4" for e in rec.evidence)


# --- review fixes -------------------------------------------------------------------

@pytest.mark.parametrize(("value", "flag", "vote"), [("yes", "unknown", Vote.AGAINST),
                                                     ("no", "yes", Vote.FOR)])
def test_analyst_org_listed_beats_the_aim_flag(value, flag, vote):
    """Rule 8 / F1: an analyst's org `listed` applies last, so the AIM flag can't undo it."""
    dec = set_fact("ig-design-group", "listed", value, kind="org", n=5)
    case = load_case("stewart_gilliland")
    rec = score_case(case, with_policy(aim_counts_as_listed=flag), (dec,))["tesco"].rec
    assert rec.vote == vote and any(e.ref == "dec:5" for e in rec.evidence)
    meeting = case.meetings["tesco"]
    rows = score.effective_seats(score_case(case, decisions=(dec,))["tesco"].seats, meeting,
                                 case.orgs, with_policy(aim_counts_as_listed=flag), [dec])
    assert next(r for r in rows if "ig-design" in r["id"])["listed"] == value


def test_effective_seats_apply_the_same_overrides_as_score():
    """Eval's seat export must agree with the vote: analyst seat values are not re-derived
    (designate chair, AIM listing) over."""
    case = load_case("steve_mogford")
    designate = next(s for s in case.seats if "conclusion" in (s.role_text or ""))
    decs = [set_fact(designate.seat_id, "chair", "yes", n=1),
            set_fact(designate.seat_id, "current", "yes", n=2)]
    scored = score_case(case, decisions=tuple(decs))["intertek"]
    assert (scored.rec.points_lo, scored.rec.points_hi) == (4, 4)
    rows = score.effective_seats(scored.seats, case.meetings["intertek"], case.orgs,
                                 load_policy(), decs)
    row = next(r for r in rows if r["id"] == designate.seat_id)
    assert (row["chair"], row["current"], row["counted"]) == ("yes", "yes", "yes")
    without = score.effective_seats(scored.seats, case.meetings["intertek"], case.orgs,
                                    load_policy())  # the old 4-argument call still works
    assert len(without) == len(rows)


@pytest.mark.parametrize(("text", "designate"), [
    ("Chair with effect from 2023", False), ("Chair", False),
    ("Chair with effect from 1 September 2026", True),
    ("Chair from the conclusion of the 2026 AGM", True)])
def test_with_effect_from_is_designate_only_after_the_agm(text, designate):
    case = load_case("steve_mogford")
    costain = case.seat("costain")
    case.seats[case.seats.index(costain)] = costain.model_copy(update={
        "role_text": text, "role_type": "chair", "chair": "yes"})
    rec = score_case(case, with_policy(chair_designate_counts_as_chair="no"))["intertek"].rec
    # Intertek 1 + QinetiQ 1 + Costain: 2 as a sitting chair, 1 as a designate (flag "no")
    assert (rec.points_lo, rec.points_hi) == ((3, 3) if designate else (4, 4))


def test_ch_record_unavailable_is_review_not_for():
    """Margaret Ewing's case: without her CH record, undisclosed seats are unknown."""
    case = load_case("gerry_murphy")
    assert score_case(case)["tesco"].rec.vote == Vote.FOR
    rec = score_case(case, ch=None)["tesco"].rec
    pid = case.person.person_id
    assert rec.vote == Vote.REVIEW
    assert f"{pid}.ch_record" in rec.unknown_facts
    assert any("CH record unavailable" in r for r in rec.reasons)


def test_unanchored_person_is_review_not_for():
    case = load_case("gerry_murphy")
    case.person = case.person.model_copy(update={"officer_ids": []})
    rec = score_case(case, ch=[])["tesco"].rec
    assert rec.vote == Vote.REVIEW
    assert f"{case.person.person_id}.ch_record" in rec.unknown_facts
    assert any("unanchored" in r for r in rec.reasons)


def test_missing_ch_record_never_hides_a_decided_against():
    rec = score_case(load_case("thierry_garnier"))["tesco"].rec
    assert rec.vote == Vote.AGAINST
    case = load_case("thierry_garnier")
    case.person = case.person.model_copy(update={"officer_ids": []})
    notice_only = score_case(case, ch=[])["tesco"].rec  # Kingfisher CEO still per the notice
    assert notice_only.vote in (Vote.AGAINST, Vote.REVIEW)


def test_anchored_available_person_has_no_ch_record_term():
    for stem in CASES:
        for scored in score_case(load_case(stem)).values():
            assert not any(f.endswith(".ch_record") for f in scored.rec.unknown_facts), stem
            assert not any("Undisclosed Companies House" in r for r in scored.rec.reasons), stem


@pytest.mark.parametrize("role, vote", [("executive_director", Vote.AGAINST),
                                        ("non_executive_director", Vote.FOR)])
def test_analyst_role_at_issuer_also_settles_the_issuer_seats_role_facts(role, vote):
    """An M&S executive with an outside listed board seat: the notice states no role, so
    the M&S seat's `executive` is unknown and the exec rule can't decide. The analyst's
    role_type_at_issuer must fill that seat's chair and executive facts, not just the
    role label (the README session found Alison Dolan stuck at REVIEW)."""
    case = load_case("fiona_dawson")
    case.seats.append(case.seats[0].model_copy(update={
        "seat_id": "seat:x:pearson", "org_name": "Pearson plc", "org_key": "pearson",
        "role_type": RoleType.non_executive_director, "board_seat": "yes", "chair": "no",
        "executive": "no", "listed": "yes"}))
    mention = case.roster[0].mention_id
    assert score_case(case)["ms"].rec.vote == Vote.REVIEW
    dec = set_fact(mention, "role_type_at_issuer", role, kind="roster", n=3)
    rec = score_case(case, decisions=(dec,))["ms"].rec
    assert rec.vote == vote, rec.reasons
    assert any(e.source == "analyst" and e.ref == "dec:3" for e in rec.evidence)
