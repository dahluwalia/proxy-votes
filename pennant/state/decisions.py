"""Analyst decisions (merge, split, set-fact, undo), stored append-only.

Design choice: a decision is never edited or deleted. Undo appends a `revoke`,
and every run replays whatever is active (`db.active_decisions`). Contradictions
are refused when the decision is written, so a run never has to choose between
two analyst instructions: a must-link that would join a cannot-link pair, even
through a chain of must-links, is refused, and so is the reverse.

Payloads (CONTRACT_CHANGES.md D1 and G1; read by cluster, score and the CLI):
- must_link   {"a": id, "b": id}: both ids end up in one person.
- cannot_link {"a": [ids], "b": [ids]}: no id in a shares a person with any id in b.
  An id is a mention id (nm:...), a CH officer id, or a person id p:<mention>,
  which is stored as that mention.
- set_fact    {"kind": "seat" | "org" | "roster", "target": seat_id | org_key | mention_id,
  "field": f, "value": v}. A roster target only takes role_type_at_issuer (M&S).
- revoke      {"decision_id": "dec:<n>"}.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from pennant.core import db
from pennant.core.models import (
    Decision,
    Evidence,
    OrgFacts,
    Person,
    Recommendation,
    RoleType,
    Seat,
)
from pennant.core.tri import to_tri

LINK_KINDS = ("must_link", "cannot_link")
TRI_FIELDS = {"board_seat", "chair", "executive", "current", "listed", "identity", "equity_listing"}
SET_FACT_FIELDS = {
    "seat": {"board_seat", "chair", "executive", "current", "listed", "identity", "role_type"},
    "org": {"listed", "equity_listing", "market", "parent_name", "org_type"},
    "roster": {"role_type_at_issuer"},
}
_KEY_ATTR = {"seat": "seat_id", "org": "org_key"}


class DecisionError(ValueError):
    """The decision can't be recorded as asked: bad payload or a contradiction."""


def node_id(x: str) -> str:
    """A person id p:<mention> means that mention."""
    return str(x).strip().removeprefix("p:")


def _ids(v: Any) -> list[str]:
    items = [v] if isinstance(v, str) else list(v or [])
    return sorted({node_id(x) for x in items if str(x).strip()})


def link_pairs(d: Decision) -> list[tuple[str, str]]:
    """Every (a, b) id pair a link decision speaks about."""
    return [(x, y) for x in _ids(d.payload.get("a")) for y in _ids(d.payload.get("b"))]


def touched(d: Decision, by_id: dict[str, Decision]) -> set[str]:
    """Ids a decision is about (a revoke is about whatever it revoked)."""
    if d.kind == "revoke":
        target = by_id.get(d.payload["decision_id"])
        return touched(target, by_id) if target else set()
    if d.kind in LINK_KINDS:
        return {*_ids(d.payload.get("a")), *_ids(d.payload.get("b"))}
    return {d.payload["target"]}


def conflicts(decisions: list[Decision]) -> list[str]:
    """Cannot-link pairs that the must-links, taken transitively, put in one person."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for d in decisions:
        if d.kind == "must_link":
            for x, y in link_pairs(d):
                parent[find(x)] = find(y)
    return [
        f"{d.decision_id} keeps {x} apart from {y}"
        for d in decisions
        if d.kind == "cannot_link"
        for x, y in link_pairs(d)
        if find(x) == find(y)
    ]


def _normalise(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind in LINK_KINDS:
        a, b = _ids(payload.get("a")), _ids(payload.get("b"))
        if not a or not b:
            raise DecisionError(f"{kind} needs ids on both sides (a and b)")
        if set(a) & set(b):
            raise DecisionError(f"{kind}: {sorted(set(a) & set(b))} is on both sides")
        if kind == "must_link":
            if len(a) != 1 or len(b) != 1:
                raise DecisionError("must_link joins exactly two ids: {'a': id, 'b': id}")
            return {"a": a[0], "b": b[0]}
        return {"a": a, "b": b}
    if kind == "set_fact":
        target_kind, field = payload.get("kind"), payload.get("field")
        target, value = str(payload.get("target") or "").strip(), payload.get("value")
        if target_kind not in SET_FACT_FIELDS:
            raise DecisionError(f"set_fact kind must be seat, org or roster, not {target_kind!r}")
        if target_kind == "roster":
            target = node_id(target)
        if field not in SET_FACT_FIELDS[target_kind]:
            allowed = ", ".join(sorted(SET_FACT_FIELDS[target_kind]))
            raise DecisionError(f"{target_kind} field must be one of {allowed}, not {field!r}")
        if not target:
            raise DecisionError("set_fact needs a target")
        try:
            if field in TRI_FIELDS:
                value = to_tri(value)
            elif field in ("role_type", "role_type_at_issuer"):
                value = RoleType(value).value
            elif value is not None:
                value = str(value)
        except ValueError as exc:
            raise DecisionError(f"bad value for {field}: {value!r}") from exc
        return {"kind": target_kind, "target": target, "field": field, "value": value}
    raise DecisionError(f"unknown decision kind: {kind!r} (use revoke() to undo)")


def add_decision(
    conn: sqlite3.Connection, kind: str, payload: dict[str, Any], author: str, reason: str
) -> Decision:
    """Validate and append a must_link, cannot_link or set_fact decision."""
    if kind == "revoke":
        return revoke(conn, payload.get("decision_id", ""), author, reason)
    if not reason or not reason.strip():
        raise DecisionError("every decision needs a reason")
    payload = _normalise(kind, payload)
    if kind in LINK_KINDS:
        draft = Decision(decision_id="(new)", kind=kind, payload=payload, author=author,
                         reason=reason, created_at=datetime.now(UTC))
        if bad := conflicts([*db.active_decisions(conn), draft]):
            raise DecisionError("contradicts active decisions: " + "; ".join(bad))
    return db.insert_decision(conn, kind, payload, author, reason)


def revoke(conn: sqlite3.Connection, decision_id: str, author: str, reason: str) -> Decision:
    """Undo a decision by appending a revoke. Undoing an undo brings the original back."""
    if not reason or not reason.strip():
        raise DecisionError("every decision needs a reason")
    everything = db.all_decisions(conn)
    by_id = {d.decision_id: d for d in everything}
    target = by_id.get(decision_id)
    if target is None:
        raise DecisionError(f"no such decision: {decision_id!r}")
    if decision_id not in _effective(everything):
        raise DecisionError(f"{decision_id} is already revoked")
    if target.kind == "revoke":
        back = by_id[target.payload["decision_id"]]
        if back.kind in LINK_KINDS and (bad := conflicts([*db.active_decisions(conn), back])):
            raise DecisionError(f"restoring {back.decision_id} contradicts: " + "; ".join(bad))
    return db.insert_decision(conn, "revoke", {"decision_id": decision_id}, author, reason)


def _effective(decisions: list[Decision]) -> set[str]:
    """Ids of decisions (revokes included) not hidden by an effective revoke."""
    revokers: dict[str, list[str]] = {}
    for d in decisions:
        if d.kind == "revoke":
            revokers.setdefault(d.payload["decision_id"], []).append(d.decision_id)
    memo: dict[str, bool] = {}

    def eff(i: str) -> bool:
        if i not in memo:
            memo[i] = not any(eff(r) for r in revokers.get(i, []))
        return memo[i]

    return {d.decision_id for d in decisions if eff(d.decision_id)}


def apply_set_facts[M: BaseModel](
    models: list[M], kind: str, decisions: list[Decision]
) -> list[M]:
    """Apply active set_fact decisions for `kind` ("seat" or "org"), oldest first so
    the newest wins. Each override adds analyst evidence naming its decision.
    Score (M6.1) may use this and write the results back; either that or citing the
    decision in a recommendation's evidence satisfies gate G4."""
    out = []
    ordered = sorted(decisions, key=decision_no)
    for m in models:
        for d in ordered:
            p = d.payload
            if d.kind != "set_fact" or p["kind"] != kind:
                continue
            if p["target"] != getattr(m, _KEY_ATTR[kind]):
                continue
            ev = Evidence(source="analyst", ref=d.decision_id, quote=d.reason,
                          fetched_at=d.created_at)
            data = m.model_dump() | {p["field"]: p["value"]}
            data["evidence"] = [*data["evidence"], ev.model_dump()]
            m = type(m).model_validate(data)
        out.append(m)
    return out


def decision_no(d: Decision) -> int:
    """dec:12 -> 12, the order decisions were made in."""
    tail = d.decision_id.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def superseded(decisions: Sequence[Decision]) -> set[str]:
    """Set-facts a newer active set-fact on the same (kind, target, field) replaces."""
    newest: dict[tuple[str, str, str], Decision] = {}
    for d in sorted((d for d in decisions if d.kind == "set_fact"), key=decision_no):
        newest[(d.payload["kind"], d.payload["target"], d.payload["field"])] = d
    keep = {d.decision_id for d in newest.values()}
    return {d.decision_id for d in decisions if d.kind == "set_fact"} - keep


def unhonoured(
    decisions: list[Decision], persons: list[Person], seats: list[Seat], orgs: list[OrgFacts],
    recs: Sequence[Recommendation] = (),
) -> list[str]:
    """Active decisions a run's outputs don't reflect (gate G4). A set_fact is honoured
    when a recommendation cites it as analyst evidence, or the stored seat or org
    holds the value. A set_fact a newer one on the same field replaces is not checked:
    the newer value is the one the run should hold."""
    where = {i: p.person_id for p in persons for i in (*p.mention_ids, *p.officer_ids)}
    models = {"seat": {s.seat_id: s for s in seats}, "org": {o.org_key: o for o in orgs}}
    cited = {e.ref for r in recs for e in r.evidence if e.source == "analyst"}
    replaced = superseded(decisions)
    problems = []
    for d in decisions:
        if d.decision_id in replaced:
            continue
        if d.kind in LINK_KINDS:
            for x, y in link_pairs(d):
                px, py = where.get(x), where.get(y)
                if d.kind == "must_link" and (px is None or px != py):
                    problems.append(f"{d.decision_id}: {x} and {y} are not one person")
                if d.kind == "cannot_link" and px is not None and px == py:
                    problems.append(f"{d.decision_id}: {x} and {y} are still one person ({px})")
        elif d.kind == "set_fact" and d.decision_id not in cited:
            p = d.payload
            target = models.get(p["kind"], {}).get(p["target"])
            if target is None:
                problems.append(f"{d.decision_id}: {p['kind']} {p['target']} is not in this "
                                "run and no recommendation applied it")
            elif getattr(target, p["field"]) != p["value"]:
                problems.append(f"{d.decision_id}: {p['target']}.{p['field']} is "
                                f"{getattr(target, p['field'])}, not {p['value']}")
    return problems
