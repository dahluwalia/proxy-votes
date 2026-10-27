"""M9.1 Hard cases: the CLAUDE.md catalog (H1–H17) checked against one stored run.

Design choice: the checks are data (`eval/gold/hard_cases.yaml`), each naming one of a few
generic check kinds, so expert can add or adjust a case without touching code. A check whose
data is missing from the run (a name not on the roster, no CH record) is N/A, not FAIL.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from eval.metrics import GOLD_DIR, aim_changes, fmt_points, pid, rec_for, tokens, unknowns


def _entries(r, name, must=True):
    found = [e for e in r["roster"] if tokens(name) <= tokens(e.name)]
    if must and not found:
        raise LookupError(f"{name} is not on the roster")
    return found


def _recs(r, spec):
    out = []
    for name in spec["names"]:
        for e in _entries(r, name):
            key = r["key"].get(e.meeting_id)
            if spec.get("meeting") in (None, key):
                if (rec := rec_for(r, e)) is None:
                    raise LookupError(f"no recommendation for {name} at {key}")
                out.append((f"{name} ({key})", rec))
    return out


def _person(r, name):
    return r["person_of"].get(_entries(r, name)[0].mention_id)


def _seats(r, name, org):
    p = _person(r, name)
    ids = set(p.mention_ids + p.officer_ids) if p else {_entries(r, name)[0].mention_id}
    found = [s for s in r["seat"] if (s.mention_id in ids or s.officer_id in ids)
             and tokens(org) <= tokens(s.org_name)]
    if not found:
        raise LookupError(f"no {org} seat for {name}")
    return found


def _org(r, name):
    found = [o for o in r["org"] if tokens(name) <= tokens(o.canonical_name)]
    if not found:
        raise LookupError(f"no org facts for {name}")
    return found[0]


def _check(conn: sqlite3.Connection, r: dict[str, Any], spec: dict[str, Any]
           ) -> tuple[bool | None, str]:
    c, exp = spec["check"], spec.get("expect")
    if c == "note":
        return None, spec["detail"]
    if c in ("vote", "points", "blocked_on"):
        got = {n: (rec.vote.value if c == "vote" else fmt_points(rec) if c == "points"
                   else unknowns(r, rec) or "none") for n, rec in _recs(r, spec)}
        ok = {n: v in exp if c == "vote" else v == exp if c == "points"
              else exp.casefold() in v.casefold() for n, v in got.items()}
        return all(ok.values()), "; ".join(f"{n}: {v}" for n, v in got.items())
    if c == "absent":
        there = [n for n in spec["names"] if _entries(r, n, must=False)]
        return not there, f"on the roster: {', '.join(there)}" if there else "none on the roster"
    if c in ("same_person", "different_people"):
        ids = {n: {pid(r, e.mention_id) for e in _entries(r, n)} for n in spec["names"]}
        ok = (all(len(v) == 1 and len(_entries(r, n)) > 1 for n, v in ids.items())
              if c == "same_person" else len(set().union(*ids.values())) == len(ids))
        return ok, "; ".join(f"{n}: {', '.join(sorted(v))}" for n, v in ids.items())
    if c == "people_count":
        n = len({pid(r, e.mention_id) for e in r["roster"]})
        return n == exp, f"{len(r['roster'])} mentions → {n} people"
    if c == "anchored":
        missing = [n for n in spec["names"] if not getattr(_person(r, n), "officer_ids", None)]
        return not missing, f"no CH record: {', '.join(missing)}" if missing else "all on CH"
    if c == "one_dob":
        officers = {o.officer_id: o for o in r["officer"]}
        p = _person(r, spec["names"][0])
        dobs = {(officers[i].dob_month, officers[i].dob_year)
                for i in p.officer_ids if i in officers}
        return len(dobs) == 1, f"{len(p.officer_ids)} CH records, dates of birth {sorted(dobs)}"
    if c == "seat":
        got = {f"{n} at {o}": {getattr(s, spec["field"]) for s in _seats(r, n, o)}
               for n, o in spec["seats"]}
        return all(v == {exp} for v in got.values()), "; ".join(
            f"{k}: {spec['field']}={'/'.join(sorted(map(str, v)))}" for k, v in got.items())
    if c == "org":
        got = {n: str(getattr(_org(r, n), spec["field"])) for n in spec["orgs"]}
        match = (lambda v: exp.casefold() in v.casefold()) if spec.get("contains") else (
            lambda v: v == exp)
        return all(match(v) for v in got.values()), "; ".join(
            f"{n}: {v}" for n, v in got.items())
    if c == "distinct_orgs":
        keys = {n: {s.org_key for s in _seats(r, spec["name"], n)} for n in spec["orgs"]}
        return not set.intersection(*keys.values()), "; ".join(
            f"{n}: {', '.join(sorted(k))}" for n, k in keys.items())
    if c == "coverage":
        got = Counter(e.coverage for e in r["roster"]
                      if r["key"].get(e.meeting_id) == spec["meeting"])
        return set(got) == {exp}, ", ".join(f"{k}: {v}" for k, v in got.items())
    if c == "aim_flag":
        rows = [a for a in aim_changes(conn, r) if tokens(spec["names"][0]) <= tokens(a["name"])]
        got = rows[0] if rows else {f: "unchanged" for f in exp}
        return all(got.get(f) == v for f, v in exp.items()), ", ".join(
            f"{f}: {got.get(f)}" for f in exp)
    raise ValueError(f"unknown check {c!r}")


def hard_cases(conn: sqlite3.Connection, r: dict[str, Any],
               path: Path = GOLD_DIR / "hard_cases.yaml") -> list[tuple[str, ...]]:
    """(id, case, PASS | FAIL | N/A, detail) for each check in hard_cases.yaml."""
    out = []
    for spec in yaml.safe_load(path.read_text()):
        try:
            ok, detail = _check(conn, r, spec)
        except LookupError as err:
            ok, detail = None, str(err)
        out.append((spec["id"], spec["case"], {True: "PASS", False: "FAIL"}.get(ok, "N/A"), detail))
    return out
