"""M4.3: clustering mentions into people, on synthetic data.

The 47-people check on the recorded CH fixtures is in test_cluster_fixtures.py.
"""

import random
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from pennant.core import db
from pennant.core.models import (
    CHAppointment,
    CHOfficer,
    Decision,
    Evidence,
    Meeting,
    ReviewItem,
    RosterEntry,
)
from pennant.dedup import cluster as cl
from pennant.dedup.cluster import Link, cluster

A, B, C, D = "nm:00000001:r4", "nm:00000002:r5", "nm:00000003:r6", "nm:00000004:r7"


def dec(n, kind, **payload):
    return Decision(
        decision_id=f"dec:{n}", kind=kind, payload=payload, author="analyst",
        reason="test", created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def groups(people):
    return sorted(p.mention_ids for p in people)


# --- pure cluster() -------------------------------------------------------------


def test_no_links_one_person_per_mention():
    people, reviews = cluster([B, A], [], [])
    assert [p.person_id for p in people] == [f"p:{A}", f"p:{B}"]
    assert reviews == []


def test_links_through_officers_join_mentions():
    links = [Link(A, "off-1", "anchor"), Link(B, "off-2", "anchor"),
             Link("off-1", "off-2", "dob")]
    people, _ = cluster([A, B, C], links, [])
    assert groups(people) == [[A, B], [C]]
    joined = people[0]
    assert joined.person_id == f"p:{A}"  # smallest notice mention id
    assert joined.officer_ids == ["off-1", "off-2"]


def test_cannot_link_keeps_a_and_c_apart():
    links = [Link(A, B, "dob"), Link(B, C, "dob")]
    people, reviews = cluster([A, B, C], links, [dec(1, "cannot_link", a=[A], b=[C])])
    assert all(not {A, C} <= set(p.mention_ids) for p in people)
    assert groups(people) == [[A, B], [C]]
    assert [r.kind for r in reviews] == ["link_refused"]
    assert "dec:1" in reviews[0].detail
    assert reviews[0].suggested_command == "pennant undo dec:1"


def test_cannot_link_blocks_indirect_join_through_officers():
    # Two mentions anchored to two CH records that a DOB link would join.
    links = [Link(A, "off-1"), Link(B, "off-2"), Link("off-1", "off-2", "dob")]
    people, reviews = cluster([A, B], links, [dec(1, "cannot_link", a=A, b=[f"p:{B}"])])
    assert groups(people) == [[A], [B]]
    assert reviews[0].kind == "link_refused" and reviews[0].subject == f"p:{A}"


def test_must_link_merges_and_is_applied_first():
    people, _ = cluster([A, B, C], [], [dec(1, "must_link", a=f"p:{A}", b=C)])
    assert groups(people) == [[A, C], [B]]


def test_must_link_wins_over_a_later_matcher_link():
    # must(A,B) and cannot(B,C): the matcher's A-C link would join C into A-B: refused.
    decisions = [dec(1, "must_link", a=A, b=B), dec(2, "cannot_link", a=[B], b=[C])]
    people, reviews = cluster([A, B, C], [Link(A, C, "dob")], decisions)
    assert groups(people) == [[A, B], [C]]
    assert reviews[0].kind == "link_refused" and "dec:2" in reviews[0].detail


def test_must_link_that_contradicts_cannot_link_is_refused():
    decisions = [dec(1, "must_link", a=A, b=B), dec(2, "cannot_link", a=[B], b=[C]),
                 dec(3, "must_link", a=A, b=C)]
    people, reviews = cluster([A, B, C], [], decisions)
    assert groups(people) == [[A, B], [C]]
    assert [(r.kind, r.subject) for r in reviews] == [("decision_conflict", "dec:3")]


def test_split_moves_mentions_out():
    # A person of three mentions; the analyst moves C out.
    links = [Link(A, "off-1"), Link(B, "off-1"), Link(C, "off-1")]
    split = dec(1, "cannot_link", a=[C], b=[A, B])
    people, reviews = cluster([A, B, C], links, [split])
    assert groups(people) == [[A, B], [C]]
    assert len(reviews) == 1


def test_stale_decision_is_flagged_not_applied():
    people, reviews = cluster([A, B], [], [dec(1, "must_link", a=A, b="nm:99999999:r1")])
    assert groups(people) == [[A], [B]]
    assert [(r.kind, r.subject) for r in reviews] == [("decision_stale", "dec:1")]


def test_shuffled_input_gives_same_output():
    links = [Link(A, "off-1"), Link(B, "off-2"), Link(C, "off-3"), Link(D, "off-1"),
             Link("off-1", "off-2", "dob"), Link("off-2", "off-3", "dob")]
    decisions = [dec(1, "cannot_link", a=[A], b=[C]), dec(2, "must_link", a=B, b="off-9")]
    expected = cluster([A, B, C, D], links, decisions)
    rng = random.Random(7)
    for _ in range(25):
        ls, ds, ms = links[:], decisions[:], [A, B, C, D]
        rng.shuffle(ls), rng.shuffle(ds), rng.shuffle(ms)
        # Links may also arrive with their ends swapped.
        ls = [Link(x.b, x.a, x.kind) if rng.random() < 0.5 else x for x in ls]
        assert cluster(ms, ls, ds) == expected


def test_anchor_links_win_over_dob_links_regardless_of_input_order():
    # A DOB link (weaker) must not decide the outcome ahead of an anchor link.
    links = [Link("off-2", "off-3", "dob"), Link(B, "off-3", "anchor"), Link(A, "off-2")]
    decisions = [dec(1, "cannot_link", a=[A], b=[B])]
    people, reviews = cluster([A, B], links, decisions)
    assert groups(people) == [[A], [B]]
    by_person = {p.person_id: p.officer_ids for p in people}
    assert by_person == {f"p:{A}": ["off-2"], f"p:{B}": ["off-3"]}
    assert "dob link" in reviews[0].detail


def test_two_mentions_in_one_notice_are_never_one_person():
    """The board has only SMITH, Andrea; 'Andrew Smith' (r4) and 'Andrea Smith' (r5) both
    anchor to her. The second anchor is refused, not merged."""
    r4, r5 = "nm:00000009:r4", "nm:00000009:r5"
    links = [Link(r4, "andrea"), Link(r5, "andrea")]
    people, [review] = cluster([r4, r5], links, [], {r4: "mtg:9", r5: "mtg:9"})
    assert groups(people) == [[r4], [r5]]
    assert review.kind == "link_refused" and "both are named in mtg:9" in review.detail
    assert review.suggested_command is None
    # without the mapping, the company number in the mention id stands for the notice
    assert groups(cluster([r4, r5], links, [])[0]) == [[r4], [r5]]


def test_analyst_must_link_overrides_the_same_notice_rule():
    """The same-notice rule only keeps the matcher apart; an analyst's merge wins."""
    r4, r5 = "nm:00000009:r4", "nm:00000009:r5"
    people, reviews = cluster([r4, r5], [], [dec(1, "must_link", a=r4, b=r5)])
    assert groups(people) == [[r4, r5]] and reviews == []


def test_analyst_cannot_link_still_beats_a_later_must_link_in_one_notice():
    r4, r5 = "nm:00000009:r4", "nm:00000009:r5"
    decisions = [dec(1, "cannot_link", a=[r4], b=[r5]), dec(2, "must_link", a=r4, b=r5)]
    people, [review] = cluster([r4, r5], [], decisions)
    assert groups(people) == [[r4], [r5]]
    assert (review.kind, review.suggested_command) == ("decision_conflict", "pennant undo dec:1")


def test_must_link_across_notices_does_not_unlock_matcher_links_inside_one():
    """Merging r4 with another notice's mention leaves r4 and r5 (same notice) apart."""
    r4, r5 = "nm:00000009:r4", "nm:00000009:r5"
    links = [Link(r4, "andrea"), Link(r5, "andrea")]
    people, [review] = cluster([r4, r5, A], links, [dec(1, "must_link", a=r4, b=A)])
    assert groups(people) == [[A, r4], [r5]] and review.kind == "link_refused"


def ev(ref):
    return Evidence(source="ch", ref=ref)


def test_person_evidence_comes_from_applied_links_in_a_fixed_order():
    links = [Link(A, "off-1", "anchor", (ev("a1"),)), Link(B, "off-2", "anchor", (ev("b2"),)),
             Link("off-1", "off-2", "dob", (ev("dob12"), ev("a1"))),  # a1 again: kept once
             Link(C, "off-2", "anchor", (ev("c2"),))]  # refused by the cannot-link
    decisions = [dec(1, "cannot_link", a=[B], b=[C])]
    expected, _ = cluster([A, B, C], links, decisions)
    by_id = {p.person_id: [e.ref for e in p.evidence] for p in expected}
    assert by_id == {f"p:{A}": ["a1", "b2", "dob12"], f"p:{C}": []}
    rng = random.Random(3)
    for _ in range(10):
        ls = links[:]
        rng.shuffle(ls)
        assert cluster([C, B, A], ls, decisions)[0] == expected


# --- run(ctx) with a stubbed M4.2 -------------------------------------------------

TESCO, ITV = "00445790", "04967001"
T_MTG, I_MTG = f"mtg:{TESCO}:2026-06-18", f"mtg:{ITV}:2026-05-07"


def officer(oid, name, *companies):
    return CHOfficer(
        officer_id=oid, name=name, dob_month=1, dob_year=1960,
        appointments=[CHAppointment(company_number=c, company_name=c, role="director",
                                    appointed_on=date(2020, 1, 1)) for c in companies],
    )


def entry(company, res, name, mtg):
    return RosterEntry(mention_id=f"nm:{company}:r{res}", meeting_id=mtg, resolution_no=res,
                       name=name, coverage="structured")


def ch_ev(oid):
    return Evidence(source="ch", ref=f"https://api.company-information.service.gov.uk/officers/{oid}/appointments")


@pytest.fixture
def stub_m42(monkeypatch):
    """A fake dedup.anchor: anchors by surname, expands Kennedy to his other record."""
    other = officer("ken-itv", "KENNEDY, Christopher", ITV, "11111111")

    def anchor(e, issuer):
        hits = [o for o in issuer if o.name.split(",")[0].lower() == e.name.split()[-1].lower()]
        if len(hits) == 1:
            return hits[0], []
        return None, [ReviewItem(kind="unanchored", subject=e.mention_id, detail=e.name)]

    def expand(o, ctx):
        return ([(other, [ch_ev(other.officer_id)])], []) if o.officer_id == "ken-tsc" else ([], [])

    fake = SimpleNamespace(anchor=anchor, expand=expand,
                           anchor_evidence=lambda e, o: [ch_ev(o.officer_id)])
    monkeypatch.setattr(cl, "matcher", fake)
    return fake


def seed(ctx, officers, reviews=()):
    db.put_outputs(ctx.conn, ctx.run_id, "meeting", [
        Meeting(meeting_id=T_MTG, company_name="Tesco PLC", company_number=TESCO,
                agm_date="2026-06-18", doc_sha="x"),
        Meeting(meeting_id=I_MTG, company_name="ITV plc", company_number=ITV,
                agm_date="2026-05-07", doc_sha="y"),
    ])
    db.put_outputs(ctx.conn, ctx.run_id, "roster", [
        entry(TESCO, 9, "Chris Kennedy", T_MTG), entry(TESCO, 13, "Caroline Silver", T_MTG),
        entry(ITV, 12, "Chris Kennedy", I_MTG), entry(ITV, 13, "Carolyn Nobody", I_MTG),
    ])
    db.put_outputs(ctx.conn, ctx.run_id, "officer", officers)
    db.put_outputs(ctx.conn, ctx.run_id, "review", list(reviews))


def test_run_writes_people_reviews_and_new_officers(ctx, stub_m42):
    seed(ctx, [officer("ken-tsc", "KENNEDY, Christopher", TESCO),
               officer("sil", "SILVER, Caroline", TESCO),
               officer("ken-itv", "KENNEDY, Christopher", ITV)])
    report = cl.run(ctx)
    assert report.ok and report.stage == "dedup"
    people = db.get_outputs(ctx.conn, ctx.run_id, "person")
    assert groups(people) == [[f"nm:{TESCO}:r13"], [f"nm:{TESCO}:r9", f"nm:{ITV}:r12"],
                              [f"nm:{ITV}:r13"]]
    kennedy = next(p for p in people if len(p.mention_ids) == 2)
    assert kennedy.person_id == f"p:nm:{TESCO}:r9"  # ids compare as strings
    assert kennedy.officer_ids == ["ken-itv", "ken-tsc"]
    assert [e.ref.rsplit("/", 2)[-2] for e in kennedy.evidence] == ["ken-itv", "ken-tsc"]
    reviews = db.get_outputs(ctx.conn, ctx.run_id, "review")
    assert [(r.kind, r.subject) for r in reviews] == [("unanchored", f"nm:{ITV}:r13")]
    assert report.counts["people"] == 3


def test_run_honours_split_decision(ctx, stub_m42):
    seed(ctx, [officer("ken-tsc", "KENNEDY, Christopher", TESCO),
               officer("ken-itv", "KENNEDY, Christopher", ITV)])
    db.insert_decision(ctx.conn, "cannot_link", {"a": [f"nm:{TESCO}:r9"], "b": [f"nm:{ITV}:r12"]},
                       "analyst", "different people")
    cl.run(ctx)
    people = db.get_outputs(ctx.conn, ctx.run_id, "person")
    assert len(people) == 4
    assert any(r.kind == "link_refused" for r in db.get_outputs(ctx.conn, ctx.run_id, "review"))


def test_run_skips_issuer_with_ch_unavailable(ctx, stub_m42):
    unavailable = ReviewItem(kind="ch_unavailable", subject=I_MTG, detail="CH 503")
    seed(ctx, [officer("ken-tsc", "KENNEDY, Christopher", TESCO)], [unavailable])
    report = cl.run(ctx)
    assert report.counts["skipped_ch_unavailable"] == 2
    kinds = [r.kind for r in db.get_outputs(ctx.conn, ctx.run_id, "review")]
    assert "unanchored" in kinds  # Silver: no Tesco record in this seed
    assert kinds.count("unanchored") == 1  # ITV entries get no extra items


def test_run_expanded_officer_is_written_once(ctx, stub_m42):
    seed(ctx, [officer("ken-tsc", "KENNEDY, Christopher", TESCO)],
         [ReviewItem(kind="ch_unavailable", subject=I_MTG, detail="CH 503")])
    cl.run(ctx)
    ids = [o.officer_id for o in db.get_outputs(ctx.conn, ctx.run_id, "officer")]
    assert ids == ["ken-itv", "ken-tsc"]


def test_run_fails_closed_when_matching_raises(ctx, stub_m42, monkeypatch):
    def boom(e, issuer):
        raise RuntimeError("bad record")

    monkeypatch.setattr(stub_m42, "anchor", boom)
    seed(ctx, [officer("ken-tsc", "KENNEDY, Christopher", TESCO)])
    report = cl.run(ctx)
    assert not report.ok and len(report.errors) == 2  # both Tesco entries
    people = db.get_outputs(ctx.conn, ctx.run_id, "person")
    assert len(people) == 4  # nothing merged on an error


def test_run_flags_entries_it_cannot_match_unless_ch_already_did(ctx, stub_m42):
    """ITV has no officers on record and no ch_unavailable item; one roster entry names a
    meeting that isn't in the run. Each gets an `unanchored` item instead of silence."""
    seed(ctx, [officer("ken-tsc", "KENNEDY, Christopher", TESCO),
               officer("sil", "SILVER, Caroline", TESCO)])
    lost = entry("00000077", 4, "Jo Bloggs", "mtg:00000077:2026-01-01")
    db.put_outputs(ctx.conn, ctx.run_id, "roster", [lost])
    cl.run(ctx)
    items = {r.subject: r.detail for r in db.get_outputs(ctx.conn, ctx.run_id, "review")
             if r.kind == "unanchored"}
    assert set(items) == {f"nm:{ITV}:r12", f"nm:{ITV}:r13", lost.mention_id}
    assert "no officers on record for 04967001" in items[f"nm:{ITV}:r12"]
    assert "not in this run" in items[lost.mention_id]


def test_run_loads_the_record_of_an_officer_only_a_merge_joined(ctx, stub_m42):
    """`pennant merge <mention> <officer_id>`: the officer's full CH record is fetched, so
    their seats are scored; if CH can't serve it, a ch_unavailable item holds gate G2."""
    full = officer("delay", "DELAY, Thomas", "02366619", "22222222")
    fetched: list[str] = []

    def fetch_officer(oid, ctx):
        fetched.append(oid)
        if oid == "delay":
            return full, []
        return None, [ReviewItem(kind="ch_unavailable", subject=oid, detail="CH 503")]

    stub_m42.fetch_officer = fetch_officer
    seed(ctx, [officer("ken-tsc", "KENNEDY, Christopher", TESCO)])
    db.insert_decision(ctx.conn, "must_link", {"a": f"nm:{TESCO}:r13", "b": "delay"}, "a", "x")
    db.insert_decision(ctx.conn, "must_link", {"a": f"nm:{ITV}:r13", "b": "gone"}, "a", "x")
    cl.run(ctx)
    assert fetched == ["delay", "gone"]  # never the matcher-linked Kennedy records
    officers = {o.officer_id: o for o in db.get_outputs(ctx.conn, ctx.run_id, "officer")}
    assert officers["delay"] == full and "gone" not in officers
    reviews = db.get_outputs(ctx.conn, ctx.run_id, "review")
    assert ("ch_unavailable", "gone") in {(r.kind, r.subject) for r in reviews}
