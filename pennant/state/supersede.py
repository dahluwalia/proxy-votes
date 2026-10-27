"""Which earlier analyst decisions a new merge or split replaces.

Design choice: the newest analyst decision wins. Two active decisions must never
contradict (a run would have to pick one), so a split that contradicts an earlier merge,
or a merge that contradicts an earlier split, first undoes the decisions it replaces,
and the CLI says which before asking. Only what is in the way goes: for a split, the
newest merge on the chain joining the two sides, repeated until they come apart; for a
merge, each split that keeps the two apart. The undo is an ordinary revoke, so the log
shows it and `pennant undo` can bring the old decision back once the new one is undone.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence

from pennant.core.models import Decision
from pennant.state.decisions import LINK_KINDS, decision_no, link_pairs


def _first_conflict(decisions: Sequence[Decision]) -> tuple[Decision, str, str] | None:
    """(cannot-link, x, y) for the first pair the must-links put in one person."""
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
    for d in decisions:
        if d.kind == "cannot_link":
            for x, y in link_pairs(d):
                if find(x) == find(y):
                    return d, x, y
    return None


def _path(must_links: Sequence[Decision], x: str, y: str) -> list[Decision]:
    """The must-links on the shortest chain joining x to y (empty if none)."""
    edges: dict[str, list[tuple[str, Decision]]] = defaultdict(list)
    for d in sorted(must_links, key=decision_no):
        for a, b in link_pairs(d):
            edges[a].append((b, d))
            edges[b].append((a, d))
    came: dict[str, tuple[str, Decision] | None] = {x: None}
    queue = deque([x])
    while queue and y not in came:
        n = queue.popleft()
        for m, d in edges[n]:
            if m not in came:
                came[m] = (n, d)
                queue.append(m)
    path, n = [], y
    while (step := came.get(n)) is not None:
        n, d = step
        path.append(d)
    return path


def replaced_by(active: Sequence[Decision], draft: Decision) -> list[Decision]:
    """The active link decisions `draft` contradicts and so replaces, in undo order."""
    live = [d for d in active if d.kind in LINK_KINDS]
    out: list[Decision] = []
    while (hit := _first_conflict([*live, draft])) is not None:
        blocker, x, y = hit
        if draft.kind == "must_link":
            pick = blocker  # an earlier split keeps the two apart
        else:
            path = _path([d for d in live if d.kind == "must_link"], x, y)
            if not path:
                break  # can't happen with a consistent log; add_decision still refuses
            pick = max(path, key=decision_no)  # the newest merge on the chain
        out.append(pick)
        live.remove(pick)
    return out
