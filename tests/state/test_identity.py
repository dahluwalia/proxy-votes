"""A merged person carries A's identity: B's mention at a shared meeting is shown as A's."""

from datetime import UTC, datetime

from pennant.core.models import Decision, Evidence, Person, RosterEntry
from pennant.dedup.cluster import cluster
from pennant.state.identity import absorbed, identity_order

A, B, X = "nm:00000009:r6", "nm:00000009:r15", "nm:00000001:r4"  # A and B: one notice


def dec(n, a, b):
    return Decision(decision_id=f"dec:{n}", kind="must_link", payload={"a": a, "b": b},
                    author="analyst", reason="test", created_at=datetime(2026, 1, 1, tzinfo=UTC))


def entry(m, meeting):
    return RosterEntry(mention_id=m, meeting_id=meeting, resolution_no=int(m.rsplit("r", 1)[1]),
                       name=m, coverage="structured")


ROSTER = {A: entry(A, "mtg:9"), B: entry(B, "mtg:9"), X: entry(X, "mtg:1")}


def test_merge_cites_its_decision_and_b_is_shown_as_a():
    d = dec(1, B, A)  # `pennant merge B A`: A takes on B's identity, whatever the ids
    [person], reviews = cluster([A, B], [], [d])
    assert reviews == [] and Evidence(source="analyst", ref="dec:1", quote="test",
                                      fetched_at=d.created_at) in person.evidence
    assert identity_order(person, {"dec:1": d}) == [B, A]
    assert absorbed([person], ROSTER, {"dec:1": d}) == {A: B}


def test_merge_across_meetings_absorbs_nothing():
    d = dec(1, A, X)
    [person], _ = cluster([A, X], [], [d])
    assert identity_order(person, {"dec:1": d}) == [A, X]
    assert absorbed([person], ROSTER, {"dec:1": d}) == {}


def test_a_person_no_analyst_merged_leads_with_its_smallest_mention():
    p = Person(person_id=f"p:{X}", mention_ids=[X, A])
    assert identity_order(p, {}) == [X, A] and absorbed([p], ROSTER, {}) == {}
