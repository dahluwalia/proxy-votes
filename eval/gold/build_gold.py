"""Build the gold evaluation set from the hand-labelled spreadsheet.

Inputs:  source/director_positions.xlsx  (Positions tab, one row per role read from the notices)
         overrides.yaml                  (meetings, roster keys, org facts, researched rows, row fixes)
         evidence.yaml                   (the source behind each label; see evidence.py)
         ../../tests/fixtures/ch/        (raw Companies House responses)
Outputs: meetings.yaml roster.yaml persons.yaml seats.yaml votes.yaml

Design choice: one process for every director (README "How it was built"). Seats come from the
notice (the spreadsheet), from Companies House (every directorship live on the AGM date), and,
where a notice has no biographies, from the company's own published board biographies. Facts
come from the same three sources, organisation listing from cited exchange or company pages.
Every label carries its evidence and the build fails without it. Expected votes are COMPUTED
from the labelled facts with the same three-valued policy logic the pipeline uses.

Usage: python build_gold.py            (writes the yaml files next to this script)
"""
from __future__ import annotations

import itertools
from datetime import date
import re
import sys
import unicodedata
from pathlib import Path

import yaml

import evidence

HERE = Path(__file__).resolve().parent
XLSX = HERE / "source" / "director_positions.xlsx"
OVERRIDES = HERE / "overrides.yaml"

DEFAULT_POLICY = {"aim_counts_as_listed": "unknown", "chair_designate_counts_as_chair": "unknown",
                  "debt_only_listing_counts": "unknown"}
ISSUER_ORG_IDS = {"itv", "intertek", "tesco", "marks-and-spencer", "severn-trent"}
FLAG_VALUES = ("yes", "no", "unknown")
THRESHOLD = 4

# ---------------------------------------------------------------- three-valued logic
def tri(v) -> str:
    """Normalise sheet/YAML values to yes | no | unknown."""
    if v is True:
        return "yes"
    if v is False:
        return "no"
    s = str(v or "").strip().lower()
    if s in ("yes", "current"):
        return "yes"
    if s in ("no", "past"):
        return "no"
    return "unknown"  # Unclear, Not stated, blank


def t_and(*xs):
    if "no" in xs:
        return "no"
    return "yes" if all(x == "yes" for x in xs) else "unknown"


def t_or(*xs):
    if "yes" in xs:
        return "yes"
    return "no" if all(x == "no" for x in xs) else "unknown"


def t_not(x):
    return {"yes": "no", "no": "yes"}.get(x, "unknown")


def seat_points(counted, chair):
    lo = 0 if counted != "yes" else (2 if chair == "yes" else 1)
    hi = 0 if counted == "no" else (1 if chair == "no" else 2)
    return lo, hi


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------------------------------------------------------------- role types
OFFICER = re.compile(
    r"\b(chief executive|ceo|cfo|coo|chief [a-z ,]*officer|president|executive vice president|"
    r"evp|managing director|finance director)"
)


def role_type(position: str, board: str, chair: str) -> str:
    p = position.lower()
    if chair == "yes":
        exec_chair = "executive chair" in p or OFFICER.search(p)
        return "executive_chair" if exec_chair else "chair"
    if "executive chair" in p:
        return "executive_chair"
    if p.startswith(("deputy chair", "vice chair")):
        return "deputy_chair"
    if "(executive director)" in p or "executive member of the board" in p:
        return "executive_director"
    if re.search(r"non-executive|independent director|senior independent|lead director|commissioner", p):
        return "non_executive_director"
    if p.startswith("chair of") and "committee" in p:
        return "committee_chair"
    if "trustee" in p:
        return "trustee"
    if re.search(r"advis|mentor", p):
        return "adviser"
    if p in ("director", "board member") or p.startswith("director ("):
        return "director_unspecified"
    if OFFICER.search(p) or "board member" in p:
        return "executive_director" if board == "yes" else "officer_title"
    return "other"


EXECUTIVE = {
    "executive_director": "yes", "executive_chair": "yes", "officer_title": "yes",
    "non_executive_director": "no", "chair": "no", "deputy_chair": "no",
    "committee_chair": "no", "adviser": "no", "trustee": "no",
}

# Rows whose listing status is stated in the sheet's Notes column (used for past roles only;
# current roles must be covered by overrides.yaml `orgs`).
LISTED_NOTE = re.compile(r"(listed|nyse|nasdaq|euronext)", re.I)
UNLISTED_NOTE = re.compile(r"(private|subsidiary|charity|public body|regulator|not-for-profit|federation|agency|not uk-listed)", re.I)


# ---------------------------------------------------------------- load
def load():
    import openpyxl  # only needed to rebuild from the spreadsheet, not to score

    ov = yaml.safe_load(OVERRIDES.read_text())
    ws = openpyxl.load_workbook(XLSX, read_only=True)["Positions"]
    rows = []
    for i, r in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not r[0]:
            continue
        director, org, position, tense, period, board, uk_listed, chair, notices, notes = r[:10]
        rows.append(dict(
            sheet_row=i, director=director.strip(), org=org.strip(), position=position.strip(),
            tense=(tense or "Unclear").strip().lower(), period=period, board=tri(board),
            uk_listed=tri(uk_listed), chair=tri(chair),
            notices=[n.strip() for n in str(notices or "").split(";") if n.strip()],
            notes=notes,
        ))
    # Researched seats the sheet lacks (see overrides.yaml `extra_seats`).
    for j, x in enumerate(ov.get("extra_seats", []), start=1):
        rows.append(dict(
            sheet_row=None, extra_id=f"W{j:02d}", director=x["director"], org=x["org"],
            position=x["position"], tense=str(x.get("tense", "current")).lower(), period=None,
            board=tri(x.get("board")), uk_listed="unknown", chair=tri(x.get("chair")),
            notices=[], notes=x.get("note"), fix=dict(basis="web"),
        ))
    return ov, rows


# ---------------------------------------------------------------- build seats
def build_seats(ov, rows):
    orgs = ov["orgs"]
    fixes = ov.get("row_fixes", {})
    seats, missing = [], []
    for r in rows:
        fx = r.get("fix") or fixes.get(r["sheet_row"], {})
        board = tri(fx["board_seat"]) if "board_seat" in fx else r["board"]
        if "chair" in fx:
            r = dict(r, chair=tri(fx["chair"]))
        rt = fx.get("role_type") or role_type(r["position"], board, r["chair"])
        current = {"current": "yes", "past": "no"}.get(r["tense"], "unknown")
        info = orgs.get(r["org"])
        if info:
            listed, market = tri(info["listed"]), info.get("market")
            org_id, org_type, parent = info["id"], info.get("type", "company"), info.get("parent")
            basis = fx.get("basis", "sheet")
        else:
            if current != "no":
                missing.append(f'row {r["sheet_row"] or r["extra_id"]}: {r["director"]} @ {r["org"]}')
            note = r["notes"] or ""
            if r["uk_listed"] == "yes" or (LISTED_NOTE.search(note) and not UNLISTED_NOTE.search(note)):
                listed = "yes"
            elif UNLISTED_NOTE.search(note):
                listed = "no"
            else:
                listed = None  # not assessed: past role, does not affect votes
            market, org_id, org_type, parent = None, slug(r["org"]), None, None
            basis = fx.get("basis", "sheet")
        notes = "; ".join(x for x in (r["notes"], fx.get("note"), info and info.get("note")) if x)
        seat = dict(
            seat_id=f'S{r["sheet_row"]:03d}' if r["sheet_row"] else r["extra_id"],
            sheet_row=r["sheet_row"], person=slug(r["director"]),
            director=r["director"], org=r["org"], org_id=org_id, org_type=org_type,
            parent_org_id=parent, position=r["position"], role_type=rt, tense=r["tense"],
            period=r["period"], board_seat=board, chair=r["chair"],
            chair_designate=bool(fx.get("chair_designate")),
            executive=EXECUTIVE.get(rt, "unknown"), listed=listed, market=market,
            debt_listed=tri((info or {}).get("debt_listed", "no")),
            uk_listed=r["uk_listed"], source_notices=r["notices"], basis=basis, notes=notes or None,
        )
        if "notice_board_seat" in fx:
            seat["notice_board_seat"] = tri(fx["notice_board_seat"])
        seats.append(seat)
    if missing:
        sys.exit("orgs missing from overrides.yaml for current roles:\n  " + "\n  ".join(missing))
    return seats


def seat_facts(s, policy, agm=None, issuer=None):
    """Policy-relevant facts for one seat under a policy setting. With an AGM date, a seat whose
    Companies House dates put it outside that date is not current there. The seat under vote
    (the issuer's own, `issuer`) counts even if it starts after the AGM (policy flag
    count_seat_under_vote), but not if the director resigned it before the AGM (Garnier)."""
    current = {"current": "yes", "past": "no"}.get(s["tense"], "unknown")
    if agm and current != "no":
        start, end = s.get("ch_from"), s.get("ch_until")
        if end and date.fromisoformat(end) <= agm:
            current = "no"
        elif s["org_id"] != issuer and start and date.fromisoformat(start) > agm:
            current = "no"
    listed = s["listed"] if s["listed"] is not None else "unknown"  # its shares
    if s["market"] and "AIM" in s["market"]:
        listed = t_and(listed, policy["aim_counts_as_listed"])
    # Listed bonds (a bank or a group's finance company) count when the policy says so, whatever
    # is known about the shares: shares unknown + bonds listed is listed yes under that policy.
    listed = t_or(listed, t_and(s.get("debt_listed", "no"),
                                policy.get("debt_only_listing_counts", "unknown")))
    chair = policy["chair_designate_counts_as_chair"] if s["chair_designate"] else s["chair"]
    # A subsidiary's seat counts like any other; group dedupe (score_person) keeps one per group.
    counted = t_and(current, s["board_seat"], listed)
    return dict(current=current, listed=listed, chair=chair, counted=counted,
                group=s["parent_org_id"] or s["org_id"])


# ---------------------------------------------------------------- Companies House and evidence
def add_ch_seats(ov, seats, ev, agms):
    """Attach each seat's CH appointment and add every live CH directorship no seat covers."""
    appts, companies = evidence.load()[1:]
    orgs = ov["orgs"]
    by_number = {i["ch_number"]: name for name, i in orgs.items() if i.get("ch_number")}
    names = {s["person"]: s["director"] for s in seats}
    extra = []
    for person, days in sorted(agms.items()):
        apps = evidence.ch_appointments(ev, appts, companies, person)
        used = set()
        for st in (x for x in seats if x["person"] == person):
            hits = [a for a in apps if evidence.match(st["org"], orgs.get(st["org"], {}).get("ch_number"), a)]
            if not hits:
                continue
            used.update(a["number"] for a in hits)
            live = [a for a in hits if any(evidence.live_on(a, d) for d in days)]
            a = (live or sorted(hits, key=lambda a: str(a["resigned_on"])))[-1]
            st["ch_evidence"] = [evidence.ch_evidence(a)]
            st["ch_from"] = str(a["appointed_on"]) if a["appointed_on"] else None
            st["ch_until"] = str(a["resigned_on"]) if a["resigned_on"] else None
            if live and st["board_seat"] == "unknown":
                st["board_seat"] = "yes"  # a CH director appointment is a board seat
            if a["company_type"] in evidence.PRIVATE_TYPES and st["listed"] in (None, "unknown"):
                st["listed"], st["listed_evidence"] = "no", [evidence.type_evidence(a)]
        for a in apps:
            if a["number"] in used or not any(evidence.live_on(a, d) for d in days):
                continue
            used.add(a["number"])
            role = (ev.get("roles") or {}).get(f'ch:{person}:{a["number"]}') or {}
            rt = role.get("capacity", "director_unspecified")
            org = by_number.get(a["number"])
            info = orgs.get(org, {})
            private = a["company_type"] in evidence.PRIVATE_TYPES
            listed = tri(info["listed"]) if info else ("no" if private else "unknown")
            extra.append(dict(
                person=person, director=names[person], org=org or a["company"],
                org_id=info.get("id") or slug(a["company"]), org_type=info.get("type", "company"),
                parent_org_id=info.get("parent"), position=f'{a["role"].title()} (Companies House)',
                role_type=rt, tense="current", period=None, board_seat="yes",
                chair="yes" if rt in ("chair", "executive_chair") else ("no" if rt != "director_unspecified" else "unknown"),
                chair_designate=False, executive=EXECUTIVE.get(rt, "unknown"), listed=listed,
                market=info.get("market"), debt_listed=tri(info.get("debt_listed", "no")),
                uk_listed="unknown", source_notices=[], basis="ch",
                notes=info.get("note"),
                ch_from=str(a["appointed_on"]) if a["appointed_on"] else None,
                ch_until=str(a["resigned_on"]) if a["resigned_on"] else None,
                ch_evidence=[evidence.ch_evidence(a)] + role.get("evidence", []),
                listed_evidence=[] if info else ([evidence.type_evidence(a)] if private else [])))
    for i, x in enumerate(sorted(extra, key=lambda x: (x["person"], x["org"])), start=1):
        seats.append(dict(seat_id=f"C{i:03d}", sheet_row=None, **x))
    return seats


def attach_evidence(seats, ev):
    """seat["evidence"]: the notice line, the CH appointment and any cited role source;
    seat["listed_evidence"]: the sources for its organisation's type and listing."""
    quotes, roles, org_ev = ev.get("notice_quotes") or {}, ev.get("roles") or {}, ev.get("orgs") or {}
    for st in seats:
        notice = [dict(source="notice", **q) for q in quotes.get(st["seat_id"], [])]
        role = roles.get(st["seat_id"]) or {}
        st["evidence"] = notice + st.pop("ch_evidence", []) + role.get("evidence", [])
        st["listed_evidence"] = org_ev.get(st["org"]) or st.get("listed_evidence") or []
        st["basis"] = "notice" if notice else ("ch" if st["basis"] == "ch" or st.get("ch_from") else "web")
    return seats


# ---------------------------------------------------------------- votes
def score_person(person_seats, role_at_meeting, policy, agm=None, issuer=None):
    facts = [(s, seat_facts(s, policy, agm, issuer)) for s in person_seats]
    groups = {}
    for s, f in facts:
        lo, hi = seat_points(f["counted"], f["chair"])
        glo, ghi = groups.get(f["group"], (0, 0))
        groups[f["group"]] = (max(glo, lo), max(ghi, hi))
    lo, hi = sum(g[0] for g in groups.values()), sum(g[1] for g in groups.values())
    gt = "yes" if lo > THRESHOLD else ("no" if hi <= THRESHOLD else "unknown")
    ned_here = {"non_executive_director": "yes", "chair": "yes", "deputy_chair": "yes",
                "executive_director": "no", "executive_chair": "no"}.get(role_at_meeting, "unknown")
    points_rule = t_and(ned_here, gt)
    exec_terms = []
    for s, f in facts:
        is_exec_seat = t_and(s["executive"], s["board_seat"], f["current"], f["listed"])
        if is_exec_seat == "no":
            continue
        others = [g["counted"] for t, g in facts if g["group"] != f["group"]]
        exec_terms.append((t_and(is_exec_seat, t_or(*others) if others else "no"), s))
    exec_rule = t_or(*[t for t, _ in exec_terms]) if exec_terms else "no"
    against = t_or(points_rule, exec_rule)
    vote = {"yes": "AGAINST", "no": "FOR"}.get(against, "REVIEW")
    counted = [dict(seat_id=s["seat_id"], org=s["org"], points=list(seat_points(f["counted"], f["chair"])))
               for s, f in facts if f["counted"] != "no"]
    exec_seat = next((s["org"] for t, s in exec_terms if t != "no"), None)
    return dict(points_lo=lo, points_hi=hi, points_rule=points_rule, exec_rule=exec_rule,
                exec_seat=exec_seat, vote=vote, counted_seats=counted)


def fragile_facts(person_seats, role, vote, agm=None, issuer=None):
    """Current-seat facts that would flip this vote if the label were wrong.

    Each current seat's board_seat, listed and current label is set to its opposite in turn
    (unknown is tried both ways); any change of vote is recorded. These are the facts worth a
    human double-check, and the ones a pipeline mistake would turn into a wrong vote.
    """
    out = []
    for i, s in enumerate(person_seats):
        # Skip what can't really be mislabelled: past roles, the seats on the five issuers' boards
        # (the notices are the record of those), and "listed" for charities, public bodies or
        # subsidiaries.
        if s["tense"] == "past" or s["org_id"] in ISSUER_ORG_IDS:
            continue
        fields = ["board_seat", "tense"]
        if s["org_type"] in ("company", None) and not s["parent_org_id"]:
            fields.append("listed")
        for field in fields:
            cur = s[field] if field != "tense" else {"current": "yes", "past": "no"}.get(s["tense"], "unknown")
            alts = {"yes": ["no"], "no": ["yes"]}.get(cur if cur is not None else "unknown", ["yes", "no"])
            for alt in alts:
                t = dict(s)
                if field == "tense":
                    t["tense"] = "current" if alt == "yes" else "past"
                else:
                    t[field] = alt
                new = score_person(person_seats[:i] + [t] + person_seats[i + 1:], role, DEFAULT_POLICY,
                                   agm, issuer)["vote"]
                if new != vote:
                    name = {"board_seat": "board seat", "listed": "listed", "tense": "current"}[field]
                    out.append(f'{s["seat_id"]} {s["org"]}: {name} {cur}->{alt} gives {new}')
    return out


def reason(r, role):
    pts = f'{r["points_lo"]}' if r["points_lo"] == r["points_hi"] else f'{r["points_lo"]}-{r["points_hi"]}'
    parts = [f'{pts} point{"" if pts == "1" else "s"} ({role.replace("_", " ")})']
    if r["exec_rule"] == "yes":
        parts.append(f'executive director of {r["exec_seat"]} (listed) with another listed seat -> executive rule')
    elif r["exec_rule"] == "unknown":
        parts.append(f'executive rule undecided ({r["exec_seat"]})')
    if r["points_rule"] == "yes":
        parts.append("over 4 points as a non-executive")
    return "; ".join(parts)


# ---------------------------------------------------------------- main
def m_agm(m):
    d = m["agm_date"]
    return d if isinstance(d, date) else date.fromisoformat(str(d))


def main():
    ov, rows = load()
    seats = build_seats(ov, rows)
    ev = evidence.load()[0]
    agm_of = {m["notice"]: m["agm_date"] for m in ov["meetings"]}
    agms = {}
    for notice, entries in ov["roster"].items():
        for res, director, *_ in entries:
            agms.setdefault(slug(director), set()).add(agm_of[notice])
    seats = attach_evidence(add_ch_seats(ov, seats, ev, agms), ev)
    problems = []
    evidence.check(seats, problems.append)
    if problems:
        sys.exit("GOLD EVIDENCE MISSING:\n  " + "\n  ".join(problems))
    for s in seats:  # policy outcome per seat, under the default flags
        f = seat_facts(s, DEFAULT_POLICY)
        s["counted"], s["points"] = f["counted"], list(seat_points(f["counted"], f["chair"]))
    by_person = {}
    for s in seats:
        by_person.setdefault(s["person"], []).append(s)

    meetings = []
    for m in ov["meetings"]:
        meetings.append(dict(m, agm_date=str(m["agm_date"]),
                             meeting_id=f'mtg:{m["company_number"]}:{m["agm_date"]}'))
    mtg = {m["notice"]: m for m in meetings}

    roster, persons, votes = [], {}, []
    for notice, entries in ov["roster"].items():
        m = mtg[notice]
        for res, director, as_written, election, bio_page, coverage in entries:
            p = slug(director)
            if p not in by_person:
                sys.exit(f"{notice}#{res}: {director} not in spreadsheet")
            issuer = [s for s in by_person[p] if s["org"] == m["company_name"] and s["tense"] == "current"]
            if not issuer:
                sys.exit(f"{notice}#{res}: no current {m['company_name']} row for {director}")
            role = issuer[0]["role_type"]
            if role == "director_unspecified":
                role = "unknown"  # M&S: title not stated in the notice
            role_label = "chair-designate" if issuer[0]["chair_designate"] else role
            key = f"{notice}#{res}"
            roster.append(dict(
                key=key, mention_id=f'nm:{m["company_number"]}:r{res}', meeting_id=m["meeting_id"],
                notice=notice, resolution_no=res, election=election, name_as_written=as_written,
                person=p, role_at_meeting=role, issuer_seat_id=issuer[0]["seat_id"],
                coverage=coverage, resolution_page=m["resolution_page"], bio_page=bio_page,
            ))
            persons.setdefault(p, dict(person=p, name=director, standing=True, mentions=[]))["mentions"].append(key)

            agm, issuer_id = m_agm(m), ov["orgs"][m["company_name"]]["id"]
            res_default = score_person(by_person[p], role, DEFAULT_POLICY, agm, issuer_id)
            variants = {}
            for aim, cd in itertools.product(FLAG_VALUES, FLAG_VALUES):
                pol = dict(DEFAULT_POLICY, aim_counts_as_listed=aim,
                           chair_designate_counts_as_chair=cd)
                variants[(aim, cd)] = score_person(by_person[p], role, pol, agm, issuer_id)["vote"]
            by_debt = {d: score_person(by_person[p], role, dict(DEFAULT_POLICY,
                                       debt_only_listing_counts=d), agm, issuer_id)["vote"]
                       for d in FLAG_VALUES}
            if len(set(by_debt.values())) > 1:  # the scorer has no answer per debt flag yet
                sys.exit(f"{key}: vote depends on debt_only_listing_counts {by_debt}: add it "
                         "to by_policy and to the scorer's flags")
            by_policy = {}
            if len({v for (a, c), v in variants.items() if c == "unknown"}) > 1:
                by_policy["aim_counts_as_listed"] = {a: variants[(a, "unknown")] for a in FLAG_VALUES}
            if len({v for (a, c), v in variants.items() if a == "unknown"}) > 1:
                by_policy["chair_designate_counts_as_chair"] = {c: variants[("unknown", c)] for c in FLAG_VALUES}

            incomplete = ov.get("incomplete", {}).get(key)
            withdrawn = (ov.get("withdrawn") or {}).get(key)
            if withdrawn and not (ev.get("withdrawn") or {}).get(key):
                sys.exit(f"{key}: withdrawn without evidence")
            entry = dict(
                key=key, mention_id=f'nm:{m["company_number"]}:r{res}', person=p, director=director,
                meeting_id=m["meeting_id"], role_at_meeting=role,
                expected_vote=None if incomplete or withdrawn else res_default["vote"],
                points_lo=res_default["points_lo"], points_hi=res_default["points_hi"],
                points_rule=res_default["points_rule"], exec_rule=res_default["exec_rule"],
                counted_seats=res_default["counted_seats"],
                reason=reason(res_default, role_label),
            )
            if by_policy:
                entry["by_policy"] = by_policy
            fragile = fragile_facts(by_person[p], role, res_default["vote"], agm, issuer_id)
            if fragile:
                entry["fragile_facts"] = fragile
            if not_in_notice := sorted({s["seat_id"] for s in by_person[p] if not s["source_notices"]
                                        and s["seat_id"] in {c["seat_id"] for c in res_default["counted_seats"]}}):
                entry["counted_seats_not_in_any_notice"] = not_in_notice
            if withdrawn:
                entry["withdrawn"] = withdrawn
                entry["withdrawn_evidence"] = ev["withdrawn"][key]
                entry["vote_had_it_been_held"] = res_default["vote"]
            if incomplete:
                entry["gold_complete"] = False
                entry["vote_on_disclosed_data"] = res_default["vote"]
                entry["incomplete"] = incomplete
            votes.append(entry)

    for name in ov["not_standing"]:
        p = slug(name)
        persons[p] = dict(person=p, name=name, standing=False, mentions=[],
                          named_in=sorted({n for s in by_person[p] for n in s["source_notices"]}))

    out = dict(meetings=meetings, roster=roster, persons=list(persons.values()), seats=seats, votes=votes)
    header = ("# GENERATED by build_gold.py from source/director_positions.xlsx + overrides.yaml.\n"
              "# Edit those, not this file.\n")
    for name, data in out.items():
        (HERE / f"{name}.yaml").write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110))
    checks(out)


# ---------------------------------------------------------------- checks (hard-case oracles)
def checks(out):
    v = {e["key"]: e for e in out["votes"]}
    fails = []

    def expect(cond, msg):
        if not cond:
            fails.append(msg)

    expect(len(out["roster"]) == 50, f'roster has {len(out["roster"])} entries, want 50')
    standing = [p for p in out["persons"] if p["standing"]]
    expect(len(standing) == 47, f"{len(standing)} standing people, want 47")
    multi = sorted(p["name"] for p in standing if len(p["mentions"]) > 1)
    expect(multi == ["Chris Kennedy", "Sharmila Nebhrajani", "Tamara Ingram"], f"cross-notice people: {multi}")
    against = sorted(k for k, e in v.items() if e["expected_vote"] == "AGAINST")
    want = sorted(["ITV#13", "Tesco#9", "ITV#12", "ITV#4", "Severn Trent#7", "Severn Trent#8", "M&S#7"])
    expect(against == want, f"AGAINST set {against}, want {want}")
    expect(v["Tesco#7"].get("withdrawn") and v["Tesco#7"]["expected_vote"] is None,
           "Garnier: resolution 7 was withdrawn, so no vote is expected")
    expect(v["Tesco#8"]["expected_vote"] == "REVIEW", "Gilliland should be REVIEW under the default AIM flag")
    expect(v["Tesco#8"]["by_policy"]["aim_counts_as_listed"] == {"yes": "AGAINST", "no": "FOR", "unknown": "REVIEW"},
           "Gilliland AIM sensitivity")
    for k in ["Tesco#10", "Severn Trent#5", "Tesco#4", "Intertek#7", "Intertek#10", "M&S#10", "Tesco#13", "M&S#8",
              "ITV#8", "Severn Trent#11"]:
        expect((v[k]["points_lo"], v[k]["points_hi"], v[k]["expected_vote"]) == (4, 4, "FOR"),
               f'{k} {v[k]["director"]}: {v[k]["points_lo"]}-{v[k]["points_hi"]} {v[k]["expected_vote"]}, want 4 FOR')
    expect((v["Intertek#12"]["points_lo"], v["Intertek#12"]["points_hi"]) == (3, 4), "Mogford chair-designate 3-4")
    expect(v["Intertek#13"]["expected_vote"] == "FOR", "Kawal Preet: FedEx EVP is not a board seat")
    expect((v["ITV#9"]["points_lo"], v["ITV#9"]["expected_vote"]) == (3, "FOR"), "Ewing: ITV + Convatec + IAG = 3")
    expect((v["M&S#5"]["points_lo"], v["M&S#5"]["expected_vote"]) == (3, "FOR"), "Norman: M&S chair + Bridgepoint = 3")
    expect(v["M&S#13"]["expected_vote"] == "FOR", "Doyle: BA is an unlisted IAG subsidiary")
    nulls = sorted(k for k, e in v.items() if e["expected_vote"] is None and not e.get("withdrawn"))
    expect(not nulls, f"incomplete gold: {nulls}")

    counts = {}
    for e in out["votes"]:
        counts[e["expected_vote"]] = counts.get(e["expected_vote"], 0) + 1
    print("votes:", {str(k): n for k, n in counts.items()})
    print(f'seats: {len(out["seats"])} rows, {sum(s["tense"] == "current" for s in out["seats"])} current, '
          f'{sum(1 for e in out["votes"] for _ in e["counted_seats"])} counted seat-mentions')
    if fails:
        sys.exit("GOLD CHECKS FAILED:\n  " + "\n  ".join(fails))
    print("all gold checks passed")


if __name__ == "__main__":
    main()
