"""A fixture database and a fake M7 for CLI tests.

The real stages may not exist yet, so `fake_run_pipeline` builds each run from the
hand-written truth below, replaying active decisions the way M4.3 and M6.1 would in
miniature: must/cannot links over mentions (CONTRACT_CHANGES D1), set_fact overrides,
then points = sum of seat_points and a NED points rule. Exec rules are given per mention.
"""

from __future__ import annotations

from datetime import date

import pytest

from pennant.core import db
from pennant.core.config import load_policy
from pennant.core.models import (
    CHAppointment,
    CHOfficer,
    Evidence,
    Meeting,
    OrgFacts,
    Person,
    Recommendation,
    ReviewItem,
    RosterEntry,
    RunContext,
    Seat,
    StageReport,
    mention_id,
    notice_ref,
)
from pennant.core.tri import imax, isum, seat_points, t_and, t_or, to_tri

TESCO, ITV = "00445790", "04967001"
MTG = {TESCO: f"mtg:{TESCO}:2026-06-18", ITV: f"mtg:{ITV}:2026-05-07"}
TESCO_PDF, ITV_PDF = "Tesco_2026_Notice_of_AGM.pdf", "ITV_2026_Notice_of_AGM.pdf"
NED, EXEC, CHAIR = "non_executive_director", "executive_director", "chair"

# (company, resolution, name, role_type, exec_rule)
ROSTER = [
    (TESCO, 4, "Melissa Bethell", NED, "no"), (TESCO, 5, "Bertrand Bodson", NED, "no"),
    (TESCO, 6, "Dame Carolyn Fairbairn", NED, "no"), (TESCO, 7, "Thierry Garnier", NED, "yes"),
    (TESCO, 8, "Stewart Gilliland", NED, "no"), (TESCO, 9, "Chris Kennedy", NED, "yes"),
    (TESCO, 10, "Dr Gerry Murphy", CHAIR, "no"), (TESCO, 11, "Ken Murphy", EXEC, "no"),
    (TESCO, 12, "Imran Nawaz", EXEC, "no"), (TESCO, 13, "Caroline Silver", NED, "no"),
    (TESCO, 14, "Karen Whitworth", NED, "no"),
    (ITV, 12, "Chris Kennedy", EXEC, "yes"), (ITV, 14, "Sharmila Nebhrajani", NED, "no"),
    (ITV, 13, "André Lacroix", None, "no"),  # synthetic: accent folding, role not stated
]
KENNEDY_T, KENNEDY_I = mention_id(TESCO, 9), mention_id(ITV, 12)
SILVER = mention_id(TESCO, 13)
BASE_LINKS = [(KENNEDY_T, KENNEDY_I)]  # what M4.2 would link through CH date of birth
OFFICERS = {"off-kennedy": KENNEDY_T}


def _seat(sid, mention, org, key, role, board, chair, current="yes", listed="yes", quote=None):
    ev = [Evidence(source="notice", ref=notice_ref(TESCO_PDF, 10), quote=quote)] if quote else []
    return Seat(seat_id=sid, mention_id=mention, org_name=org, org_key=key, role_text=role,
                board_seat=board, chair=chair, executive="no", current=current, listed=listed,
                identity="yes", status="confirmed", evidence=ev)


SILVER_SEATS = [
    _seat("seat:silver:barratt", SILVER, "Barratt Redrow plc", "barratt-redrow", "Chair", "yes",
          "yes", quote="Chair: Barratt Redrow plc and ICE Clear Europe"),
    _seat("seat:silver:ice", SILVER, "Intercontinental Exchange, Inc.", "ice",
          "Non-executive director", "yes", "no"),
    _seat("seat:silver:moelis", SILVER, "Moelis & Company", "moelis", "Other", "unknown", "no",
          quote="Other: International Advisory Board of Adobe Inc, board of trustees of V&A "
                "Foundation, Moelis & Company"),
]
ORGS = [OrgFacts(org_key="ice", canonical_name="Intercontinental Exchange, Inc.", listed="yes",
                 market="NYSE", evidence=[Evidence(source="web", ref="https://ir.theice.com/",
                                                   title="ICE investors", quote="NYSE: ICE")]),
        OrgFacts(org_key="moelis", canonical_name="Moelis & Company", listed="yes",
                 market="NYSE")]


def truth_meetings() -> list[Meeting]:
    return [Meeting(meeting_id=MTG[TESCO], company_name="Tesco PLC", company_number=TESCO,
                    agm_date=date(2026, 6, 18), doc_sha="t", key="tesco"),
            Meeting(meeting_id=MTG[ITV], company_name="ITV plc", company_number=ITV,
                    agm_date=date(2026, 5, 7), doc_sha="i")]  # no key: CLI falls back by number


def truth_roster() -> list[RosterEntry]:
    out = []
    for co, res, name, role, _ in ROSTER:
        pdf = TESCO_PDF if co == TESCO else ITV_PDF
        out.append(RosterEntry(
            mention_id=mention_id(co, res), meeting_id=MTG[co], resolution_no=res, name=name,
            role_text_at_issuer=role and role.replace("_", " "), role_type_at_issuer=role,
            coverage="structured",
            evidence=[Evidence(source="notice", ref=notice_ref(pdf, 4), quote=name)]))
    return out


def issuer_seats() -> list[Seat]:
    return [Seat(seat_id=f"seat:{mention_id(co, res)}:issuer", mention_id=mention_id(co, res),
                 org_name="Tesco PLC" if co == TESCO else "ITV plc", org_key=co,
                 role_type=role or "other",
                 board_seat="yes", chair="yes" if role == CHAIR else "no", current="yes",
                 listed="yes", identity="yes", status="confirmed")
            for co, res, _, role, _ in ROSTER]


def _clusters(decisions) -> list[list[str]]:
    mentions = [mention_id(co, res) for co, res, *_ in ROSTER]
    parent = {m: m for m in mentions}

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    def as_list(v):
        return [v] if isinstance(v, str) else list(v)

    cannot = [(set(as_list(d.payload["a"])), set(as_list(d.payload["b"])))
              for d in decisions if d.kind == "cannot_link"]
    musts = [(d.payload["a"], d.payload["b"]) for d in decisions if d.kind == "must_link"]
    for a, b in musts + BASE_LINKS:
        a, b = a.removeprefix("p:"), b.removeprefix("p:")
        if a not in parent or b not in parent:  # a CH officer id: no person to join here
            continue
        ra, rb = find(a), find(b)
        group = {m for m in mentions if find(m) in (ra, rb)}
        if not any(group & x and group & y for x, y in cannot):
            parent[ra] = rb
    groups: dict[str, list[str]] = {}
    for m in mentions:
        groups.setdefault(find(m), []).append(m)
    return [sorted(g) for g in groups.values()]


def fixture_stage(ctx) -> StageReport:
    """Write the whole truth as one stage's outputs, replaying active decisions."""
    conn, run_id = ctx.conn, ctx.run_id
    decisions = db.active_decisions(conn)
    seats = _apply_facts(issuer_seats() + SILVER_SEATS, "seat", "seat_id", decisions)
    orgs = _apply_facts(ORGS, "org", "org_key", decisions)
    persons = [Person(person_id=f"p:{g[0]}", mention_ids=g,
                      officer_ids=[o for o, m in OFFICERS.items() if m in g])
               for g in _clusters(decisions)]
    exec_rule = {mention_id(co, res): ex for co, res, _, _, ex in ROSTER}
    role = {mention_id(co, res): r for co, res, _, r, _ in ROSTER}
    cited: dict[str, list[Evidence]] = {}  # roster set_facts, cited by the rec (G4)
    for d in decisions:
        if d.kind == "set_fact" and d.payload["kind"] == "roster":
            role[d.payload["target"]] = d.payload["value"]
            cited.setdefault(d.payload["target"], []).append(
                Evidence(source="analyst", ref=d.decision_id, quote=d.reason))
    recs = []
    for p in persons:
        mine = [s for s in seats if s.mention_id in p.mention_ids]
        by_org = {}
        for s in mine:
            iv = seat_points(t_and(s.board_seat, s.current, s.listed, s.identity), s.chair)
            by_org.setdefault(s.org_key, []).append(iv)
        points = isum(imax(v) for v in by_org.values())
        for m in p.mention_ids:
            ned = "unknown" if role[m] is None else "yes" if role[m] in (NED, CHAIR) else "no"
            pr, er = t_and(ned, points.gt(4)), exec_rule[m]
            v = t_or(pr, er)
            vote = {"yes": "AGAINST", "no": "FOR"}.get(v, "REVIEW")
            unknown = [f"{s.seat_id}.board_seat" for s in mine if s.board_seat == "unknown"]
            recs.append(Recommendation(
                person_id=p.person_id, meeting_id=next(e.meeting_id for e in truth_roster()
                                                       if e.mention_id == m),
                role_at_meeting=role[m], points_lo=points.lo, points_hi=points.hi,
                points_rule=pr, exec_rule=er, vote=vote, coverage="structured",
                unknown_facts=unknown if vote == "REVIEW" else [], evidence=cited.get(m, [])))
    db.put_outputs(conn, run_id, "meeting", truth_meetings())
    db.put_outputs(conn, run_id, "roster", truth_roster())
    db.put_outputs(conn, run_id, "seat", seats)
    db.put_outputs(conn, run_id, "org", orgs)
    db.put_outputs(conn, run_id, "officer", [CHOfficer(
        officer_id="off-kennedy", name="KENNEDY, Christopher", dob_month=3, dob_year=1964,
        appointments=[CHAppointment(company_number=n, company_name=c, role="director")
                      for n, c in ((TESCO, "TESCO PLC"), (ITV, "ITV PLC"))])])
    db.put_outputs(conn, run_id, "person", persons)
    db.put_outputs(conn, run_id, "recommendation", recs)
    db.put_outputs(conn, run_id, "review", [ReviewItem(
        kind="unknown_fact", subject="seat:silver:moelis", detail="Moelis role not stated",
        suggested_command='pennant set-fact seat:silver:moelis board_seat no --reason "..."')])
    return StageReport(stage="fixture", ok=True, counts={"recommendation": len(recs)})


def fake_run_pipeline(settings, offline, publish, author, status="staged"):
    """Stands in for state.pipeline.run_pipeline; stores the gate report in its format."""
    conn = db.connect(settings.db_path)
    run_id = db.create_run(conn, "fixture", author)
    fixture_stage(RunContext(conn=conn, run_id=run_id, settings=settings, policy=load_policy(),
                             offline=offline))
    ok = {"ok": True, "skipped": False, "problems": []}
    db.finish_run(conn, run_id, status, {
        "gates": {"G1": ok, "G2": ok, "G4": ok},
        "decisions": [d.decision_id for d in db.active_decisions(conn)]})
    if publish and status == "staged":
        db.publish(conn, run_id)
    conn.close()
    return run_id


def _apply_facts(models, kind, key, decisions):
    """set_fact payload {"kind", "target", "field", "value"}, as state/decisions.py reads it."""
    out = []
    for m in models:
        for d in decisions:
            p = d.payload
            if d.kind == "set_fact" and p["kind"] == kind and p["target"] == getattr(m, key):
                ev = Evidence(source="analyst", ref=d.decision_id, quote=d.reason)
                m = m.model_copy(update={p["field"]: p["value"], "evidence": [*m.evidence, ev]})
        out.append(m)
    return out


FACT_FIELDS = {"seat": {"board_seat", "chair", "executive", "current", "listed", "identity"},
               "org": {"listed", "equity_listing"}, "roster": {"role_type_at_issuer"}}


def fake_add_decision(conn, kind, payload, author, reason):
    """Validates the way state/decisions.add_decision does, in miniature."""
    if kind == "set_fact":
        if payload["field"] not in FACT_FIELDS[payload["kind"]]:
            raise ValueError(f"bad field {payload['field']!r}")
        if payload["kind"] != "roster":
            payload = payload | {"value": to_tri(payload["value"])}
    if kind == "must_link":
        for d in db.active_decisions(conn):
            if d.kind == "cannot_link" and {payload["a"], payload["b"]} <= {
                    *d.payload["a"], *d.payload["b"]}:
                raise ValueError(f"contradicts {d.decision_id}")
    return db.insert_decision(conn, kind, payload, author, reason)


def fake_revoke(conn, decision_id, author, reason):
    if decision_id not in {d.decision_id for d in db.all_decisions(conn)}:
        raise ValueError(f"no such decision: {decision_id}")
    if decision_id not in {d.decision_id for d in db.active_decisions(conn)}:
        raise ValueError(f"{decision_id} is already revoked")
    return db.insert_decision(conn, "revoke", {"decision_id": decision_id}, author, reason)


def fake_diff_text(conn, run_a, run_b):
    """Decisions that differ between the runs, then people and votes that changed."""
    da, db_ = (set((db.get_run(conn, r)["gate_report"] or {}).get("decisions", []))
               for r in (run_a, run_b))
    lines = [f"decision {d} applied" for d in sorted(db_ - da)]
    lines += [f"decision {d} no longer applied" for d in sorted(da - db_)]
    pa, pb = ({p.person_id: p.mention_ids for p in db.get_outputs(conn, r, "person")}
              for r in (run_a, run_b))
    lines += [f"person {p} removed {pa[p]}" for p in sorted(pa) if pa[p] != pb.get(p)]
    lines += [f"person {p} added {pb[p]}" for p in sorted(pb) if pb[p] != pa.get(p)]
    ra, rb = ({(r.person_id, r.meeting_id): r for r in db.get_outputs(conn, x, "recommendation")}
              for x in (run_a, run_b))
    for k in sorted(set(ra) & set(rb)):
        if ra[k].vote != rb[k].vote:
            lines.append(f"{k[0]} at {k[1]}: {ra[k].vote} -> {rb[k].vote}")
    return "\n".join([f"{run_a} → {run_b}", *lines]) if lines else f"no changes from {run_a}"


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """A file database with one published fixture run; the CLI's M7 hooks are faked."""
    import pennant.cli as cli

    path = tmp_path / "pennant.db"
    monkeypatch.setenv("PENNANT_DB", str(path))
    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(cli, "add_decision", fake_add_decision)
    monkeypatch.setattr(cli, "revoke", fake_revoke)
    monkeypatch.setattr(cli, "diff_text", fake_diff_text)
    settings = cli.load_settings()
    assert settings.db_path == str(path)
    fake_run_pipeline(settings, offline=True, publish=True, author="system")
    return path



@pytest.fixture
def lane_g_db(tmp_path, monkeypatch):
    """The real state.pipeline / decisions / diff, with the stage list swapped for
    fixture_stage. Skips until lane G's modules exist."""
    pipeline = pytest.importorskip("pennant.state.pipeline")
    pytest.importorskip("pennant.state.diff")
    path = tmp_path / "pennant.db"
    monkeypatch.setenv("PENNANT_DB", str(path))
    monkeypatch.setattr(pipeline, "STAGES", (("fixture", fixture_stage),))
    return path
