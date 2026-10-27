"""Run the stages, apply the gates, and publish only through them.

Design choice: a run never edits an earlier run. It writes its own run_outputs,
records the inputs it saw (notice hashes, policy, and the cache and decision
high-water marks) in its report, and ends staged, held or failed. The published
pointer moves only when every gate passes, so a bad night (CH down, a model
wobble) leaves the last published votes standing.

Gates (a stage that reports ok=False fails the gate of its step: extract G1,
ch G2, the rest G4):
- G1 extraction: resolution numbers contiguous per meeting; every notice quote
  behind an AGAINST still grounds on its PDF page.
- G2 CH: no CH unavailability in this run, issuer- or officer-level (any review
  item whose kind contains `unavailable`), and each issuer has officers on record.
  An officer-level outage hides that director's other seats, which can turn a
  REVIEW into a FOR, so it holds the run too (H16): yesterday's votes stand.
- G3 churn (system runs only): no FOR<->AGAINST flip against the published run
  unless a changed input touches that person (a decision about them, their notice,
  a cache entry for their CH records or their orgs' research, or a policy change).
- G4 completeness: every roster entry has a recommendation, every active
  decision is honoured.

Rollback: `pennant rollback run:N` moves the pointer back to an earlier good run. While
that rollback is in effect (db.rolled_back_from), every run is staged, not published, so
neither the next nightly run nor a re-run after an analyst decision (both read the cache,
which may still hold what went wrong) can undo it; it ends when an analyst publishes a
newer run.

A gate that raises fails the run. Offline runs first seed the CH and web caches
from the committed fixtures, for keys the cache doesn't hold yet. Each run writes its
own log file (pennant/logs.py: stage times and counts, errors with tracebacks, the work
done and AI tokens spent), and its report keeps the timings and counters.
"""

from __future__ import annotations

import importlib
import sqlite3
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from pennant import logs
from pennant.core import db
from pennant.core.cache import get_cache, put_cache
from pennant.core.config import NOTICES_DIR, Policy, Settings, config_hash, load_policy
from pennant.core.models import (
    Evidence,
    Recommendation,
    RunContext,
    StageReport,
    Vote,
    parse_notice_ref,
)
from pennant.extract.meeting import gap_problem
from pennant.state.decisions import node_id, unhonoured
from pennant.state.inputs import (
    explain,
    input_changes,
    input_index,
    load_outputs,
    max_id,
    outages,
    recs_by_mention,
    run_inputs,
    stored_inputs,
    vote_keys,
)

log = logs.get("pipeline")

StageFn = Callable[[RunContext], StageReport]
STAGES: tuple[tuple[str, str], ...] = (
    ("extract", "pennant.extract.validate"),
    ("ch", "pennant.ch.client"),
    ("dedup", "pennant.dedup.cluster"),
    ("classify", "pennant.classify.research"),
    ("score", "pennant.policy.score"),
    ("evidence", "pennant.evidence.freshness"),  # dates citations, flags stale ones
)
STAGE_GATE = {"extract": "G1", "ch": "G2"}  # any other stage's failure fails G4
CH_UNAVAILABLE = "ch_unavailable"  # review kind; on a meeting id it means the issuer list
# Offline runs seed these cache namespaces from each module's load_fixtures(conn).
OFFLINE_SEEDS: tuple[tuple[str, str], ...] = (
    ("ch", "pennant.ch.client"),
    ("web", "pennant.classify.websearch"),
)


class GateResult(BaseModel):
    ok: bool
    skipped: bool = False
    problems: list[str] = []


class RunResult(BaseModel):
    run_id: str
    status: str  # staged, held, published, failed
    previous_published: str | None  # the published run before this one, for diffing
    stages: list[StageReport] = []
    gates: dict[str, GateResult] = {}
    error: str | None = None


def run_pipeline(
    settings: Settings,
    offline: bool = False,
    publish: bool = False,
    author: str = "system",
    *,
    conn: sqlite3.Connection | None = None,
    policy: Policy | None = None,
    stages: list[tuple[str, StageFn | str]] | None = None,
) -> RunResult:
    """One run: stages in order, then gates; publish only if asked and every gate passes.
    Runs by anyone but "system" are analyst re-runs and skip G3."""
    own = conn is None
    conn = conn or db.connect(settings.db_path)
    try:
        return _run(conn, settings, policy or load_policy(), offline, publish, author,
                    list(stages or STAGES))
    finally:
        if own:
            conn.close()


def _run(conn, settings, policy, offline, publish, author, stages) -> RunResult:
    previous = db.published_run_id(conn)
    run_id = db.create_run(conn, config_hash(policy, settings), author)
    logs.reset()
    with logs.run_log(logs.log_path(conn, run_id)) as log_path:
        return _stages_and_gates(conn, settings, policy, offline, publish, author, stages,
                                 previous, run_id, log_path)


def _stages_and_gates(conn, settings, policy, offline, publish, author, stages, previous,
                      run_id, log_path) -> RunResult:
    ctx = RunContext(conn=conn, run_id=run_id, settings=settings, policy=policy, offline=offline)
    result = RunResult(run_id=run_id, status="failed", previous_published=previous)
    log.info("%s started by %s (%s; published run %s)", run_id, author,
             "offline" if offline else "live", previous or "none")
    try:
        if offline:
            log.info("seeded %d cache rows from recorded fixtures", seed_offline_cache(conn))
    except Exception as exc:  # fail closed: a broken fixture fails the run
        log.exception("seeding the offline cache failed")
        result.error = f"seeding offline cache: {type(exc).__name__}: {exc}"
        stages = []
    inputs = run_inputs(conn, policy, settings)
    reports: dict[str, StageReport] = {}
    timings: dict[str, float] = {}
    for name, target in stages:
        log.info("stage %s: started", name)
        start = time.monotonic()
        try:
            fn = importlib.import_module(target).run if isinstance(target, str) else target
            rep = fn(ctx)
            if not isinstance(rep, StageReport):
                raise TypeError(f"run(ctx) returned {type(rep).__name__}, not StageReport")
        except Exception as exc:  # fail closed: any stage error fails the run
            timings[name] = round(time.monotonic() - start, 2)
            log.exception("stage %s: failed after %.1fs", name, timings[name])
            result.error = f"stage {name}: {type(exc).__name__}: {exc}"
            break
        timings[name] = round(time.monotonic() - start, 2)
        reports[name] = rep
        log.info("stage %s: %s in %.1fs: %s", name, "ok" if rep.ok else "NOT OK",
                 timings[name], ", ".join(f"{k}={v}" for k, v in rep.counts.items()))
        for err in rep.errors:
            log.warning("stage %s: %s", name, err)
    result.stages = list(reports.values())
    inputs["cache_max_id"] = max_id(conn, "cache")
    if result.error is None:
        try:
            result.gates = check_gates(conn, run_id, reports, inputs, previous,
                                       system=author == "system")
            result.status = "staged" if all(g.ok for g in result.gates.values()) else "held"
        except Exception as exc:  # fail closed: a gate that can't decide fails the run
            log.exception("gates failed")
            result.gates, result.status = {}, "failed"
            result.error = f"gates: {type(exc).__name__}: {exc}"
    for gate, g in result.gates.items():
        (log.info if g.ok else log.warning)("gate %s: %s%s", gate, "pass" if g.ok else "FAIL",
                                            "".join(f"; {x}" for x in g.problems))
    # After a rollback, runs are staged but not published until someone publishes one
    # explicitly (`pennant publish`): the next night, or a re-run from cached answers that
    # may hold what went wrong, can't undo the rollback.
    paused = db.rolled_back_from(conn) if publish else None
    if paused and result.status == "staged":
        log.warning("not publishing: %s was rolled back to %s; check this run with "
                    "`pennant diff`, then `pennant publish %s`", paused, previous, run_id)
    metrics = logs.snapshot()
    db.finish_run(conn, run_id, result.status, {
        "paused": (f"rolled back from {paused} to {previous}; runs are staged, not published, "
                   "until one is published with `pennant publish`") if paused else None,
        "inputs": inputs,
        "stages": [r.model_dump() for r in result.stages],
        "gates": {k: g.model_dump() for k, g in result.gates.items()},
        "error": result.error,
        "timings": timings,
        "metrics": metrics,
        "log": str(log_path),
    })
    if publish and result.status == "staged" and not paused:
        db.publish(conn, run_id)
        result.status = "published"
    ai = logs.ai_totals(metrics)
    log.info("work: %s", ", ".join(f"{k}={v}" for k, v in metrics.items()
                                    if not k.startswith("ai.")) or "none")
    for where, t in ai.items():
        if t:
            log.info("AI (%s): %d calls, %d input tokens, %d output tokens, %d web searches",
                     where, t.get("calls", 0), t.get("input_tokens", 0),
                     t.get("output_tokens", 0), t.get("web_searches", 0))
    log.info("%s finished: %s in %.1fs%s", run_id, result.status, sum(timings.values()),
             f" ({result.error})" if result.error else "")
    return result


def seed_offline_cache(conn: sqlite3.Connection) -> int:
    """Load the committed CH and web fixtures for keys this cache has no row for, so
    an offline run on a fresh database has the recorded answers. The loaders append
    unconditionally, so they write into a scratch database and only missing keys are
    copied: nothing is duplicated and no newer live entry is shadowed. LlamaExtract
    recordings are seeded by the extract stage itself. Returns rows added."""
    added = 0
    for namespace, module in OFFLINE_SEEDS:
        scratch = db.connect(":memory:")
        try:
            importlib.import_module(module).load_fixtures(scratch)
            have = {k for (k,) in conn.execute(
                "SELECT DISTINCT key FROM cache WHERE namespace = ?", (namespace,))}
            keys = [k for (k,) in scratch.execute(
                "SELECT DISTINCT key FROM cache WHERE namespace = ? ORDER BY key", (namespace,))]
            for key in keys:
                if key not in have and (hit := get_cache(scratch, namespace, key)):
                    put_cache(conn, namespace, key, hit[0], hit[1])
                    added += 1
        finally:
            scratch.close()
    return added


# --- gates -------------------------------------------------------------------


def check_gates(conn, run_id, reports, inputs, previous, system: bool) -> dict[str, GateResult]:
    out = load_outputs(conn, run_id)
    voted = recs_by_mention(out)
    return {
        "G1": _gate(reports, "G1", _extraction_problems(out)),
        "G2": _gate(reports, "G2", _ch_problems(out)),
        "G3": _gate(reports, "G3", _churn_problems(conn, out, inputs, previous))
        if system else GateResult(ok=True, skipped=True),
        "G4": _gate(reports, "G4", [
            *(f"{r.mention_id}: no recommendation" for r in out["roster"]
              if r.mention_id not in voted),
            *unhonoured(db.active_decisions(conn), out["person"], out["seat"], out["org"],
                        out["recommendation"]),
        ]),
    }


def _gate(reports: dict[str, StageReport], gate: str, problems: list[str]) -> GateResult:
    failed = [f"stage {name} not ok: {'; '.join(r.errors) or 'no detail'}"
              for name, r in reports.items() if STAGE_GATE.get(name, "G4") == gate and not r.ok]
    return GateResult(ok=not (failed or problems), problems=failed + problems)


def _extraction_problems(out: dict[str, list[Any]]) -> list[str]:
    problems = []
    nos: dict[str, list[int]] = {m.meeting_id: [] for m in out["meeting"]}
    for r in out["roster"]:
        nos.setdefault(r.meeting_id, []).append(r.resolution_no)
    for mid, ns in sorted(nos.items()):
        if why := gap_problem(ns):
            problems.append(f"{mid}: {why}")
    against = [r for r in out["recommendation"] if r.vote == Vote.AGAINST]
    for rec in against:
        for ev in evidence_behind(rec, out):
            if ev.source == "notice" and ev.quote and not quote_grounded(ev):
                problems.append(f"{rec.person_id} AGAINST rests on ungrounded quote "
                                f"{ev.ref}: {ev.quote!r}")
    return problems


def evidence_behind(rec: Recommendation, out: dict[str, list[Any]]) -> list[Evidence]:
    """The recommendation's evidence plus that of the person's roster entries and seats.
    A roster entry no person covers is scored on its own as p:<mention> (F1), so its
    own roster quote and seats count too."""
    person = next((p for p in out["person"] if p.person_id == rec.person_id), None)
    ids = set(person.mention_ids + person.officer_ids) if person else {node_id(rec.person_id)}
    roster = [r for r in out["roster"] if r.mention_id in ids]
    seats = [s for s in out["seat"] if s.mention_id in ids or s.officer_id in ids]
    return [*rec.evidence, *(e for x in [*roster, *seats] for e in x.evidence)]


def quote_grounded(ev: Evidence) -> bool:
    """Does the quote appear on its cited notice page (or the pages either side)?
    Fails closed: a missing PDF or page reference counts as ungrounded."""
    from pennant.evidence.grounding import ground_near

    doc, page = parse_notice_ref(ev.ref)
    path = NOTICES_DIR / doc
    if page is None or not path.is_file():
        return False
    return ground_near(_pages(str(path), path.stat().st_mtime), page, ev.quote or "")[0]


@lru_cache(maxsize=8)
def _pages(path: str, mtime: float) -> list[str]:  # mtime: part of the cache key only
    from pennant.evidence.grounding import page_texts

    return page_texts(path)


def _ch_problems(out: dict[str, list[Any]]) -> list[str]:
    problems = [f"{r.subject}: {r.kind}: {r.detail}" for r in outages(out)]
    on_record = {a.company_number.zfill(8) for o in out["officer"] for a in o.appointments}
    problems += [f"{m.meeting_id}: no Companies House officers on record for "
                 f"{m.company_name} ({m.company_number})"
                 for m in out["meeting"] if m.company_number not in on_record]
    return problems


def _churn_problems(conn, out, inputs: dict, previous: str | None) -> list[str]:
    """A FOR<->AGAINST flip is excused only by a changed input that touches that
    person (diff.vote_keys against diff.input_index); one refreshed web entry for
    someone else's org excuses nothing."""
    if previous is None:
        return []
    decided = (Vote.FOR, Vote.AGAINST)
    old = load_outputs(conn, previous)
    before = recs_by_mention(old)
    flips = [(m, f"{m}: {before[m].vote} → {rec.vote}")
             for m, rec in sorted(recs_by_mention(out).items())
             if m in before and {before[m].vote, rec.vote} == set(decided)]
    if not flips:
        return []
    index = input_index(conn, input_changes(conn, stored_inputs(conn, previous), inputs))
    return [f"{f} with no changed input for this person since {previous}" for m, f in flips
            if not explain(index, vote_keys(old, m) | vote_keys(out, m))]
