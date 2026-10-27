"""What a run's answers depend on, and which changed inputs could have moved a vote.

Design choice: each run records its inputs (notice hashes, the policy, and the cache and
decision high-water marks) in its gate report, so any two runs can be compared without
re-running anything. Gate G3 and the diff share one vocabulary for what a vote rests on
(`vote_keys`) and what a changed input touches (`input_index`): mention, officer, seat
and org ids, `co:<company number>`, `notice:<meeting key>`, `name:<officer name>`. An
input of a shape this can't place touches every vote, so it never hides a change.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from pennant.ch.parse import natural_name
from pennant.core import db
from pennant.core.cache import get_cache
from pennant.core.config import NOTICES_DIR, Policy, Settings, meeting_key_for
from pennant.core.models import OUTPUT_MODELS, Recommendation, ReviewItem
from pennant.extract.backends import all_backends
from pennant.state.decisions import touched

# --- run inputs and outputs -----------------------------------------------------


def max_id(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]


def run_inputs(conn: sqlite3.Connection, policy: Policy, settings: Settings) -> dict[str, Any]:
    """What a run's answers depend on, recorded in its report so G3 and diff can
    name what changed. The pipeline sets cache_max_id again when the run ends."""
    return {
        "notices": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(NOTICES_DIR.glob("*.pdf"))},
        "policy": policy.model_dump() | {"llm_model": settings.llm_model},
        "decision_max_id": max_id(conn, "decisions"),
        "cache_max_id": max_id(conn, "cache"),
    }


def stored_inputs(conn: sqlite3.Connection, run_id: str | None) -> dict[str, Any]:
    run = db.get_run(conn, run_id) if run_id else None
    return ((run or {}).get("gate_report") or {}).get("inputs") or {}


def input_changes(conn: sqlite3.Connection, a: dict, b: dict) -> dict[str, list[str]]:
    """Inputs that differ between two runs: notices, policy flags, decisions made in
    between (revokes included) and cache keys whose newest payload changed."""
    na, nb = a.get("notices", {}), b.get("notices", {})
    pa, pb = a.get("policy", {}), b.get("policy", {})
    lo, hi = sorted((a.get("decision_max_id", 0), b.get("decision_max_id", 0)))
    decisions = [r[0] for r in conn.execute(
        "SELECT decision_id FROM decisions WHERE id > ? AND id <= ? ORDER BY id", (lo, hi))]
    return {
        "notices": sorted(n for n in na.keys() | nb.keys() if na.get(n) != nb.get(n)),
        "policy": [f"{k}: {pa.get(k)} → {pb.get(k)}"
                   for k in sorted(pa.keys() | pb.keys()) if pa.get(k) != pb.get(k)],
        "decisions": decisions,
        "cache": _cache_changes(conn, a.get("cache_max_id", 0), b.get("cache_max_id", 0)),
    }


def _cache_changes(conn: sqlite3.Connection, x: int, y: int) -> list[str]:
    lo, hi = sorted((x, y))
    if lo == hi:
        return []
    old: dict[tuple[str, str], str] = {}
    new: dict[tuple[str, str], str] = {}
    for row_id, ns, key, payload in conn.execute(
        "SELECT id, namespace, key, payload FROM cache WHERE id <= ? ORDER BY id", (hi,)
    ):
        if row_id <= lo:
            old[(ns, key)] = payload
        new[(ns, key)] = payload
    return sorted(f"{ns}:{key}" for (ns, key), p in new.items() if old.get((ns, key)) != p)


def load_outputs(conn: sqlite3.Connection, run_id: str | None) -> dict[str, list[Any]]:
    return {k: db.get_outputs(conn, run_id, k) if run_id else [] for k in OUTPUT_MODELS}


def recs_by_mention(out: dict[str, list[Any]]) -> dict[str, Recommendation]:
    """Each roster mention's recommendation (via its person and meeting)."""
    person_of = {m: p.person_id for p in out["person"] for m in p.mention_ids}
    recs = {(r.meeting_id, r.person_id): r for r in out["recommendation"]}
    # a roster entry no person covers is scored on its own, as p:<mention> (F1)
    found = {r.mention_id: recs.get((r.meeting_id, person_of.get(r.mention_id,
                                                                 f"p:{r.mention_id}")))
             for r in out["roster"]}
    return {m: rec for m, rec in found.items() if rec is not None}


def outages(out: dict[str, list[Any]]) -> list[ReviewItem]:
    """Review items saying CH couldn't answer, issuer- or officer-level: every kind
    containing `unavailable` (the CH and dedup stages write `ch_unavailable`), which
    is how score reads them too (F1)."""
    return [r for r in out["review"] if "unavailable" in r.kind]


# --- what a vote rests on, and which changed inputs touch it (gate G3) ---------


def _co(number: Any) -> str:
    return f"co:{str(number).strip().upper().zfill(8)}"


def _name(name: str) -> str:
    return "name:" + " ".join(sorted(re.findall(r"[a-z0-9]+", name.casefold())))


def vote_keys(out: dict[str, list[Any]], mention: str) -> set[str]:
    """What a roster mention's vote rests on: its person's mentions and CH officer ids,
    their seats and orgs, every company those name, the CH names searched for, and the
    notice."""
    ids = {mention, *person_ids(out, mention)}
    seats = [s for s in out["seat"] if s.mention_id in ids or s.officer_id in ids]
    officers = [o for o in out["officer"] if o.officer_id in ids]
    meetings = {r.meeting_id for r in out["roster"] if r.mention_id in ids}
    keys = ids | {s.seat_id for s in seats} | {s.org_key for s in seats}
    keys |= {_co(s.ch_company_number) for s in seats if s.ch_company_number}
    keys |= {_co(a.company_number) for o in officers for a in o.appointments}
    keys |= {_name(natural_name(o)) for o in officers}
    for m in out["meeting"]:
        if m.meeting_id in meetings:
            keys |= {_co(m.company_number), f"notice:{m.key}"}
    return keys


def input_index(conn: sqlite3.Connection, changes: dict[str, list[str]]
                ) -> list[tuple[str, set[str] | None]]:
    """(cause, keys it touches) for every changed input (see `input_changes`), in
    `vote_keys`' vocabulary. None touches every vote: a policy change, or a cache entry
    of a shape this can't place."""
    by_id = {d.decision_id: d for d in db.all_decisions(conn)}
    index: list[tuple[str, set[str] | None]] = [(f"policy {p}", None) for p in changes["policy"]]
    index += [(i, touched(by_id[i], by_id)) for i in changes["decisions"] if i in by_id]
    index += [(f"notice changed: {n}", {f"notice:{meeting_key_for(n)}"})
              for n in changes["notices"]]
    extracts = _extract_keys() if any(not k.startswith(("ch:", "web:"))
                                      for k in changes["cache"]) else {}
    index += [(f"cache {k}", _cache_keys(conn, k, extracts)) for k in changes["cache"]]
    return index


def explain(index: list[tuple[str, set[str] | None]], keys: set[str]) -> list[str]:
    """The changed inputs in `index` that touch any of `keys`."""
    return [cause for cause, touches in index if touches is None or touches & keys]


def _cache_keys(conn: sqlite3.Connection, entry: str, extracts: dict[str, str]
                ) -> set[str] | None:
    ns, key = entry.split(":", 1)
    if ns == "ch":  # the key is the URL
        url = urlsplit(key)
        parts = [unquote(p) for p in url.path.strip("/").split("/")]
        if parts[0] == "company" and len(parts) > 1:  # profile or officer list
            return {_co(parts[1])}
        if parts[0] == "officers" and len(parts) > 1:  # one officer's appointments
            return {parts[1]}
        if parts[:2] == ["search", "officers"]:
            return {_name(q) for q in parse_qs(url.query).get("q", [])}
    elif ns == "web":  # the payload records the org it researched
        hit = get_cache(conn, "web", key)
        inputs = hit[0].get("inputs") if hit and isinstance(hit[0], dict) else None
        if isinstance(inputs, dict) and inputs.get("org_key"):
            return {inputs["org_key"]}
    elif entry in extracts:
        return {f"notice:{extracts[entry]}"}
    elif ns in {b.NAMESPACE for b in all_backends()}:
        return set()  # an extract of a PDF no longer in the folder feeds no vote
    return None


def _extract_keys() -> dict[str, str]:
    """Extract cache key -> meeting key, for every notice in the folder and every
    extract backend (extract/backends.py)."""
    return {f"{b.NAMESPACE}:{b.notice_key(p.read_bytes())}": meeting_key_for(p.name)
            for b in all_backends() for p in sorted(NOTICES_DIR.glob("*.pdf"))}


def person_ids(out: dict[str, list[Any]], mention: str) -> set[str]:
    p = next((p for p in out["person"] if mention in p.mention_ids), None)
    return {*p.mention_ids, *p.officer_ids} if p else set()
