"""How fresh each fetched fact is: dates on every Companies House and web citation, and a
review item when a citation was last checked before the day its vote needs it for.

Design choice: an answer counts from the last fetch that confirmed it, not the last time
it changed. A CH appointment list unchanged since April but fetched again last night is
current. A citation is stale when that check is before min(today, AGM date). An AGM still
ahead needs today's data. One already held needs data from on or after the meeting,
because that data shows every appointment and resignation up to it. A record that serves
two meetings is held to the later one. Notice and analyst citations are not fetched data,
so they are never stale.

This stage runs after scoring and reads the cache rows behind each CH citation, so a run
stores the dates it actually used. A stale citation is flagged for the analyst; it does
not change a vote.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pennant.ch.client import BASE_URL, WEB_URL
from pennant.core import db
from pennant.core.models import Evidence, ReviewItem, RunContext, StageReport

FETCHED = ("ch", "web")  # sources whose answers can go out of date
LABEL = {"ch": "Companies House", "web": "Web"}


def local_day(when: datetime) -> date:
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone().date()


def checked(ev: Evidence) -> datetime | None:
    """When this answer was last confirmed. A web answer is never refetched unchanged,
    so its fetch date is its last check."""
    return ev.checked_at or ev.fetched_at


def need_from(agm_dates: Iterable[date], today: date) -> date | None:
    """The day a record must have been checked on or after: min(today, its latest AGM)."""
    agms = list(agm_dates)
    return min(today, max(agms)) if agms else None


def is_stale(ev: Evidence, need: date | None) -> bool:
    """Fetched data last checked before `need`, or with no date at all (fail closed)."""
    if ev.source not in FETCHED or need is None:
        return False
    when = checked(ev)
    return when is None or local_day(when) < need


# --- dates from the cache ---------------------------------------------------------


def _start_index(key: str) -> int | None:
    values = parse_qs(urlsplit(key).query).get("start_index")
    return int(values[0]) if values and values[0].isdigit() else None


def ch_dates(conn: sqlite3.Connection, ref: str) -> tuple[datetime, datetime] | None:
    """(last updated, last checked) of the cached CH answer behind a citation URL: the
    newest row for that API URL, or for each page of a paged list. Last updated is the
    latest of the pages, and last checked the earliest, because a list is only as fresh as
    its oldest page. Pages past the list's current length are ignored; they are left over
    from when the list was longer. None if nothing is cached."""
    if not ref.startswith(WEB_URL):
        return None
    api = BASE_URL + ref[len(WEB_URL):]
    prefix = api + "?"
    newest: dict[str, tuple[str, str, str]] = {}
    for key, payload, updated, last in conn.execute(
            "SELECT key, payload, fetched_at, last_fetched FROM cache WHERE namespace = 'ch' "
            "AND (key = ? OR substr(key, 1, ?) = ?) ORDER BY id", (api, len(prefix), prefix)):
        newest[key] = (payload, updated, last or updated)
    total = None
    for key, (payload, _, _) in newest.items():
        if _start_index(key) == 0:
            total = (json.loads(payload).get("body") or {}).get("total_results")
    rows = [(u, c) for key, (_, u, c) in newest.items()
            if (start := _start_index(key)) in (None, 0) or total is None or start < int(total)]
    if not rows:
        return None
    return (max(datetime.fromisoformat(u) for u, _ in rows),
            min(datetime.fromisoformat(c) for _, c in rows))


def stamp(evidence: list[Evidence], dates: Any) -> list[Evidence]:
    """CH citations dated from the cache (`dates(ref)` -> (updated, checked) or None)."""
    out = []
    for ev in evidence:
        found = dates(ev.ref) if ev.source == "ch" else None
        if found:
            ev = ev.model_copy(update={"fetched_at": found[0], "checked_at": found[1]})
        out.append(ev)
    return out


# --- stage ------------------------------------------------------------------------


def _review(subject: str, ev: Evidence, need: date, agms: list[date]) -> ReviewItem:
    when = checked(ev)
    why = (f"before the AGM on {need}" if need in agms else f"before today ({need}), and "
           f"the AGM on {max(agms)} is still ahead")
    seen = f"last checked {local_day(when)}" if when else "has no fetch date"
    return ReviewItem(
        kind="stale_evidence", subject=subject,
        detail=f"{LABEL[ev.source]} {ev.ref} {seen}, {why}; facts from it may be out of date",
        suggested_command="pennant run")


def flagged(items: Iterable[ReviewItem]) -> set[tuple[str, str]]:
    """(subject, citation URL) for each stale_evidence item, so a view marks exactly what
    the run flagged."""
    out = set()
    for i in items:
        if i.kind != "stale_evidence":
            continue
        for label in LABEL.values():
            if i.detail.startswith(label + " "):
                out.add((i.subject, i.detail[len(label) + 1:].split(" ", 1)[0]))
    return out


def run(ctx: RunContext, today: date | None = None) -> StageReport:
    """Date the CH citations on this run's people, seats and orgs; flag the stale ones.
    Rewrites only the records whose evidence gained a date."""
    today = today or date.today()
    out = {k: db.get_outputs(ctx.conn, ctx.run_id, k)
           for k in ("meeting", "roster", "person", "seat", "org")}
    agm = {m.meeting_id: m.agm_date for m in out["meeting"]}
    meeting_of = {r.mention_id: r.meeting_id for r in out["roster"]}
    owner: dict[str, Any] = {}  # mention or officer id -> person
    for p in out["person"]:
        owner.update({i: p for i in [*p.mention_ids, *p.officer_ids]})

    def agms_of(mention_ids: Iterable[str]) -> list[date]:
        return sorted({agm[meeting_of[m]] for m in mention_ids
                       if m in meeting_of and meeting_of[m] in agm})

    cache: dict[str, Any] = {}

    def dates(ref: str) -> Any:
        if ref not in cache:
            cache[ref] = ch_dates(ctx.conn, ref)
        return cache[ref]

    flags: dict[tuple[str, str], ReviewItem] = {}

    def check(subject: str, evidence: list[Evidence], agms: list[date]) -> None:
        need = need_from(agms, today)
        for ev in evidence:
            if is_stale(ev, need) and (subject, ev.ref) not in flags:
                flags[(subject, ev.ref)] = _review(subject, ev, need, agms)

    changed: dict[str, list[Any]] = {"person": [], "seat": [], "org": []}
    org_agms: dict[str, set[date]] = {}
    for s in out["seat"]:
        p = owner.get(s.mention_id or "") or owner.get(s.officer_id or "")
        agms = agms_of(p.mention_ids if p else [s.mention_id] if s.mention_id else [])
        org_agms.setdefault(s.org_key, set()).update(agms)
        ev = stamp(s.evidence, dates)
        if ev != s.evidence:
            changed["seat"].append(s.model_copy(update={"evidence": ev}))
        check(p.person_id if p else s.seat_id, ev, agms)
    for p in out["person"]:
        ev = stamp(p.evidence, dates)
        if ev != p.evidence:
            changed["person"].append(p.model_copy(update={"evidence": ev}))
        check(p.person_id, ev, agms_of(p.mention_ids))
    for o in out["org"]:
        ev = stamp(o.evidence, dates)
        if ev != o.evidence:
            changed["org"].append(o.model_copy(update={"evidence": ev}))
        check(o.org_key, ev, sorted(org_agms.get(o.org_key, ())))
    for kind, models in changed.items():
        db.put_outputs(ctx.conn, ctx.run_id, kind, models)
    items = [flags[k] for k in sorted(flags)]
    db.put_outputs(ctx.conn, ctx.run_id, "review", items)
    counts = {f"dated_{k}s": len(v) for k, v in changed.items()}
    counts |= {"stale_citations": len(items)}
    return StageReport(stage="evidence", ok=True, counts=counts)
