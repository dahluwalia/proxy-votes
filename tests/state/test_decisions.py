"""M7.1: decisions are append-only, validated, and never contradict each other."""

import pytest

from pennant.core import db
from pennant.core.models import Evidence, OrgFacts, Person, Recommendation, Seat
from pennant.state.decisions import (
    DecisionError,
    add_decision,
    apply_set_facts,
    conflicts,
    revoke,
    unhonoured,
)


def link(conn, kind, a, b, reason="r"):
    return add_decision(conn, kind, {"a": a, "b": b}, "expert", reason)


def fact(conn, target, field, value, kind="seat", reason="r"):
    return add_decision(conn, "set_fact", {"kind": kind, "target": target, "field": field,
                                           "value": value}, "expert", reason)


def test_link_payloads_are_normalised(conn):
    m = link(conn, "must_link", "p:nm:1:r4", "nm:2:r4")
    assert m.payload == {"a": "nm:1:r4", "b": "nm:2:r4"}  # person id means its mention
    c = link(conn, "cannot_link", "nm:3:r1", ["nm:3:r2", "p:nm:3:r3"])
    assert c.payload == {"a": ["nm:3:r1"], "b": ["nm:3:r2", "nm:3:r3"]}
    assert m.decision_id == "dec:1" and c.decision_id == "dec:2"


@pytest.mark.parametrize("payload", [
    {"a": "x"}, {"a": "x", "b": "x"}, {"a": ["x", "y"], "b": "z"}, {"a": [], "b": "z"}])
def test_bad_must_link_payloads_refused(conn, payload):
    with pytest.raises(DecisionError):
        add_decision(conn, "must_link", payload, "expert", "r")
    assert db.all_decisions(conn) == []


def test_reason_required(conn):
    with pytest.raises(DecisionError, match="reason"):
        link(conn, "must_link", "a", "b", reason="  ")


def test_must_link_against_active_cannot_link_refused(conn):
    link(conn, "cannot_link", "a", ["b"])
    with pytest.raises(DecisionError, match="dec:1 keeps a apart from b"):
        link(conn, "must_link", "b", "a")


def test_must_link_refused_through_a_chain(conn):
    link(conn, "must_link", "a", "b")
    link(conn, "cannot_link", ["a"], ["c"])
    with pytest.raises(DecisionError):
        link(conn, "must_link", "b", "c")  # a-b-c would join a and c


def test_cannot_link_inside_a_must_link_group_refused(conn):
    link(conn, "must_link", "a", "b")
    link(conn, "must_link", "b", "c")
    with pytest.raises(DecisionError):
        link(conn, "cannot_link", ["c"], ["a"])


def test_revoked_cannot_link_no_longer_blocks(conn):
    c = link(conn, "cannot_link", "a", "b")
    revoke(conn, c.decision_id, "expert", "wrong")
    assert link(conn, "must_link", "a", "b").decision_id == "dec:3"
    assert [d.decision_id for d in db.active_decisions(conn)] == ["dec:3"]


def test_revoke_rules(conn):
    m = link(conn, "must_link", "a", "b")
    with pytest.raises(DecisionError, match="no such decision"):
        revoke(conn, "dec:99", "expert", "r")
    undo = revoke(conn, m.decision_id, "expert", "undo")
    with pytest.raises(DecisionError, match="already revoked"):
        revoke(conn, m.decision_id, "expert", "again")
    revoke(conn, undo.decision_id, "expert", "undo the undo")
    assert [d.decision_id for d in db.active_decisions(conn)] == [m.decision_id]
    undone = add_decision(conn, "revoke", {"decision_id": m.decision_id}, "expert", "r")
    assert undone.kind == "revoke"


def test_undo_of_undo_refused_when_it_would_contradict(conn):
    m = link(conn, "must_link", "a", "b")
    undo = revoke(conn, m.decision_id, "expert", "undo")
    link(conn, "cannot_link", "a", "b")
    with pytest.raises(DecisionError, match="restoring dec:1"):
        revoke(conn, undo.decision_id, "expert", "redo")


def test_set_fact_validation(conn):
    assert fact(conn, "seat:x", "board_seat", False).payload["value"] == "no"
    assert fact(conn, "seat:x", "role_type", "adviser").payload["value"] == "adviser"
    assert fact(conn, "org:y", "market", "AIM", kind="org").payload["value"] == "AIM"
    ms = fact(conn, "p:nm:04256886:r5", "role_type_at_issuer", "non_executive_director",
              kind="roster")
    assert ms.payload["target"] == "nm:04256886:r5"  # M&S: role not stated in the notice
    for target, field, value, kind in [("seat:x", "vote", "FOR", "seat"),
                                       ("seat:x", "board_seat", "maybe", "seat"),
                                       ("seat:x", "role_type", "boss", "seat"),
                                       ("", "chair", "yes", "seat"),
                                       ("p:1", "chair", "yes", "person"),
                                       ("nm:1:r5", "chair", "yes", "roster")]:
        with pytest.raises(DecisionError):
            fact(conn, target, field, value, kind=kind)


def test_apply_set_facts_newest_wins_with_analyst_evidence(conn):
    fact(conn, "s1", "board_seat", "yes")
    d2 = fact(conn, "s1", "board_seat", "no", reason="advisory role")
    fact(conn, "o1", "listed", "yes", kind="org")
    seats = [Seat(seat_id="s1", org_name="Moelis", org_key="o1"),
             Seat(seat_id="s2", org_name="Other", org_key="o2")]
    out = apply_set_facts(seats, "seat", db.active_decisions(conn))
    assert out[0].board_seat == "no" and out[1] == seats[1]
    assert out[0].evidence[-1].source == "analyst"
    assert out[0].evidence[-1].ref == d2.decision_id
    assert out[0].evidence[-1].quote == "advisory role"
    orgs = apply_set_facts([OrgFacts(org_key="o1", canonical_name="Moelis")], "org",
                           db.active_decisions(conn))
    assert orgs[0].listed == "yes"


def test_unhonoured(conn):
    link(conn, "must_link", "m1", "m2")
    link(conn, "cannot_link", "m3", "m4")
    fact(conn, "s1", "chair", "yes")
    fact(conn, "gone", "chair", "yes")
    persons = [Person(person_id="p:m1", mention_ids=["m1"]),
               Person(person_id="p:m2", mention_ids=["m2"]),
               Person(person_id="p:m3", mention_ids=["m3", "m4"])]
    seats = [Seat(seat_id="s1", org_name="X", org_key="x", chair="no")]
    problems = unhonoured(db.active_decisions(conn), persons, seats, [])
    assert problems == [
        "dec:1: m1 and m2 are not one person",
        "dec:2: m3 and m4 are still one person (p:m3)",
        "dec:3: s1.chair is no, not yes",
        "dec:4: seat gone is not in this run and no recommendation applied it",
    ]
    cites = Recommendation(person_id="p:m1", meeting_id="mtg", points_lo=0, points_hi=0,
                           points_rule="no", exec_rule="no", vote="FOR", coverage="none",
                           evidence=[Evidence(source="analyst", ref="dec:3"),
                                     Evidence(source="analyst", ref="dec:4")])
    assert unhonoured(db.active_decisions(conn), persons, seats, [], [cites]) == problems[:2]
    persons = [Person(person_id="p:m1", mention_ids=["m1", "m2"]),
               Person(person_id="p:m3", mention_ids=["m3"]),
               Person(person_id="p:off", mention_ids=[], officer_ids=["m4"])]
    seats = [seats[0].model_copy(update={"chair": "yes"}),
             Seat(seat_id="gone", org_name="Y", org_key="y", chair="yes")]
    assert unhonoured(db.active_decisions(conn), persons, seats, []) == []


def test_conflicts_is_empty_for_consistent_sets(conn):
    link(conn, "must_link", "a", "b")
    link(conn, "cannot_link", "a", "c")
    assert conflicts(db.active_decisions(conn)) == []


def test_superseded_set_fact_is_not_unhonoured(conn):
    """A newer active set-fact on the same (kind, target, field) replaces an older one:
    G4 checks only the newer value, and apply_set_facts applies it in any order."""
    old = fact(conn, "s1", "board_seat", "yes")
    new = fact(conn, "s1", "board_seat", "no")
    fact(conn, "s1", "chair", "yes")  # another field on the same seat is still checked
    seat = Seat(seat_id="s1", org_name="Moelis", org_key="o1", chair="no")
    newest_last = db.active_decisions(conn)
    applied = apply_set_facts([seat], "seat", newest_last[::-1])  # order mustn't matter
    assert applied[0].board_seat == "no" and applied[0].evidence[-2].ref == new.decision_id
    assert unhonoured(newest_last, [], applied, []) == []
    holds_new = seat.model_copy(update={"board_seat": "no"})
    assert unhonoured(newest_last, [], [holds_new], []) == ["dec:3: s1.chair is no, not yes"]
    stale = seat.model_copy(update={"board_seat": "yes", "chair": "yes"})
    assert unhonoured(newest_last, [], [stale], []) == [
        f"{new.decision_id}: s1.board_seat is yes, not no"]
    revoke(conn, new.decision_id, "expert", "back to the first answer")
    assert unhonoured(db.active_decisions(conn), [], [stale], []) == []
    assert old.decision_id == "dec:1"
