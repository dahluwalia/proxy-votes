"""Whose identity a merged person carries.

Design choice: when an analyst merges A and B (`pennant merge A B`), B takes on A's
identity: the person shows A's name and, at a meeting where both stand, one vote, on
A's resolution and role, over both people's seats. Nothing new is stored for this.
The cluster stage cites each applied must-link as analyst evidence on the person, and
decisions are never edited, so the direction (a, then b) can be read back for any run.
`pennant undo` revokes the must-link and the two come apart on the next run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pennant.core.models import Decision, Person, RosterEntry


def _dec_no(decision_id: str) -> int:
    tail = decision_id.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def identity_order(person: Person, decisions: Mapping[str, Decision]) -> list[str]:
    """The person's mention ids, the one whose identity they carry first. Each must-link
    the person cites, oldest first, puts A's side ahead of B's; mentions no merge
    touched follow, smallest id first (a matcher-built person leads with its smallest)."""
    groups: dict[str, list[str]] = {}  # root node -> its nodes, identity order
    root: dict[str, str] = {}

    def find(n: str) -> str:
        if n not in root:
            root[n], groups[n] = n, [n]
        while root[n] != n:
            n = root[n]
        return n

    refs = {e.ref for e in person.evidence if e.source == "analyst"}
    for ref in sorted(refs, key=_dec_no):
        d = decisions.get(ref)
        if d is None or d.kind != "must_link":
            continue
        ra = find(str(d.payload["a"]).removeprefix("p:"))
        rb = find(str(d.payload["b"]).removeprefix("p:"))
        if ra != rb:
            root[rb] = ra
            groups[ra] += groups.pop(rb)
    mine = set(person.mention_ids)
    ordered = [n for g in sorted(groups.values()) for n in g if n in mine]
    if ordered:  # the group holding the smallest merged mention decides
        lead = groups[find(min(ordered))]
        ordered = [n for n in lead if n in mine] + [n for n in ordered if n not in lead]
    return ordered + sorted(mine - set(ordered))


def absorbed(
    persons: Iterable[Person], roster: Mapping[str, RosterEntry],
    decisions: Mapping[str, Decision],
) -> dict[str, str]:
    """{B's mention: A's mention} wherever a merged person has two or more mentions at
    one meeting. Only A's is scored and shown there; B's resolution takes A's vote."""
    out: dict[str, str] = {}
    for p in persons:
        if len(p.mention_ids) < 2:
            continue
        lead: dict[str, str] = {}  # meeting id -> leading mention there
        for m in identity_order(p, decisions):
            if m not in roster:
                continue
            first = lead.setdefault(roster[m].meeting_id, m)
            if first != m:
                out[m] = first
    return out
