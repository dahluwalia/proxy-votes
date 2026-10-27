"""M9.1 Metrics: how often the pipeline is right, and where it isn't.

Design choice: the gold set (`eval/gold/`, hand-labelled, see its README) ships its own
scorer, so this module never re-implements grading: it exports one stored run as CSVs
(`eval/export.py`), hands them to `eval/gold/score.py`, and reports what comes back. The
headline is wrong AGAINST votes, target zero. Nothing here assumes which notices were run:
entries outside the gold set are reported as not covered, and with no gold set the report
still describes the run. AIM re-scores the run under each flag value without writing. Hard
cases are data (`eval/gold/hard_cases.yaml`), checked against the same run.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pennant.core import db
from pennant.core.config import ROOT, Policy, load_policy, load_settings
from pennant.policy.stage import rescore

if not __package__:  # loaded by file path (`pennant eval`): make `eval.*` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.export import export_run, write_roster, write_votes  # noqa: E402

GOLD_DIR = ROOT / "eval" / "gold"
REPORT = ROOT / "eval" / "report.md"
GOLD_FLAGS = ("aim_counts_as_listed", "chair_designate_counts_as_chair", "debt_only_listing_counts")
KINDS = ("meeting", "roster", "person", "recommendation", "seat", "org", "officer", "review")


# --- the run -------------------------------------------------------------------

def tokens(name: str) -> set[str]:
    s = unicodedata.normalize("NFKD", name or "")
    return set(re.findall(r"[a-z0-9]+", "".join(c for c in s if not unicodedata.combining(c))
                          .casefold()))


def load_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    r: dict[str, Any] = {k: db.get_outputs(conn, run_id, k) for k in KINDS}
    r["run_id"] = run_id
    r["key"] = {m.meeting_id: m.key or m.company_name for m in r["meeting"]}
    r["person_of"] = {m: p for p in r["person"] for m in p.mention_ids}
    r["rec"] = {(x.meeting_id, x.person_id): x for x in r["recommendation"]}
    r["seat_by_id"] = {x.seat_id: x for x in r["seat"]}
    return r


def describe(r: dict[str, Any], fact: str) -> str:
    """'seat:00445790:r13:7.board_seat' -> 'Moelis & Company board_seat (`seat:...:7`)'."""
    target, _, field = fact.rpartition(".")
    seat = r["seat_by_id"].get(target)
    what = seat.org_name if seat else "role at this meeting" if target.startswith("nm:") else ""
    return f"{what} {field} (`{target}`)".strip()


def unknowns(r: dict[str, Any], rec: Any) -> str:
    return ", ".join(describe(r, f) for f in rec.unknown_facts)


def pid(r: dict[str, Any], mention: str) -> str:
    p = r["person_of"].get(mention)
    return p.person_id if p else f"p:{mention}"  # score scores an unclustered entry alone


def rec_for(r: dict[str, Any], entry: Any) -> Any:
    return r["rec"].get((entry.meeting_id, pid(r, entry.mention_id)))


def fmt_points(rec: Any) -> str:
    return "?" if rec is None else (str(rec.points_lo) if rec.points_lo == rec.points_hi
                                    else f"{rec.points_lo}–{rec.points_hi}")


def _connect(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn or db.connect(load_settings().db_path)


def _run_id(conn: sqlite3.Connection, run_id: str | None) -> str:
    run_id = run_id or db.published_run_id(conn)
    if run_id is None:
        raise RuntimeError("no published run yet: run `pennant run --publish` first")
    return run_id


# --- gold ----------------------------------------------------------------------

def load_scorer(gold_dir: Path = GOLD_DIR) -> Any | None:
    """The gold set's own scorer (`<gold_dir>/score.py`), loaded by path and pointed at
    `gold_dir`; None when there is no gold set."""
    path = gold_dir / "score.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(f"gold_score_{gold_dir.name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HERE = gold_dir  # its load_gold() reads the yaml next to HERE
    return mod


def gold_flags(policy: Policy) -> dict[str, str]:
    """The policy flags the gold votes depend on (the scorer picks gold's answer per flag)."""
    return {f: str(getattr(policy, f)) for f in GOLD_FLAGS}


def grade(conn: sqlite3.Connection, r: dict[str, Any], policy: Policy, scorer: Any,
          csv_dir: Path) -> tuple[dict[str, Any], list[str], list[tuple[str, dict]]]:
    """Export the run, grade it against gold, then re-grade it under each AIM value
    (re-scored without writing; gold answers under the same value)."""
    out = export_run(csv_dir, r, policy, db.active_decisions(conn))
    m, lines = scorer.score(out, gold_flags(policy))
    by_flag = []
    for flag in ("unknown", "yes", "no"):
        pol = policy.model_copy(update={"aim_counts_as_listed": flag})
        d = csv_dir / f"aim-{flag}"
        write_roster(d, r)
        write_votes(d, r, rescore(conn, r["run_id"], pol))
        by_flag.append((flag, scorer.score(d, gold_flags(pol))[0]))
    return m, lines, by_flag


def coverage(r: dict[str, Any], scorer: Any) -> tuple[int, list[str]]:
    """How many of this run's roster entries the gold set covers, and the meetings it
    doesn't (the pipeline takes any notice; gold labels only some)."""
    known = scorer.load_gold()["key_of"]
    covered = {e.meeting_id for e in r["roster"] if e.mention_id in known}
    return (sum(e.mention_id in known for e in r["roster"]),
            sorted(r["key"][m] for m in r["key"] if m not in covered))


def aim_changes(conn: sqlite3.Connection, r: dict[str, Any]) -> list[dict[str, str]]:
    """Directors whose vote depends on aim_counts_as_listed (re-scored, nothing written)."""
    base = load_policy()
    by = {flag: {(x.meeting_id, x.person_id): x.vote.value for x in rescore(
        conn, r["run_id"], base.model_copy(update={"aim_counts_as_listed": flag}))}
        for flag in ("unknown", "yes", "no")}
    out = []
    for e in r["roster"]:
        k = (e.meeting_id, pid(r, e.mention_id))
        if len({by[f].get(k) for f in by}) > 1:
            out.append({"name": e.name, "meeting": r["key"].get(e.meeting_id, "?"),
                        **{f: by[f].get(k, "?") for f in by}})
    return out


# --- report --------------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def main(conn: sqlite3.Connection | None = None, run_id: str | None = None,
         gold_dir: Path = GOLD_DIR, out: Path = REPORT, csv_dir: Path | None = None) -> str:
    """Write eval/report.md for the published run (or `run_id`) and return its text. The
    run's CSV export, which the gold scorer grades, goes to `csv_dir` (eval/out/)."""
    conn = _connect(conn)
    r = load_run(conn, _run_id(conn, run_id))
    policy = load_policy()
    pred = {e.mention_id: (rec.vote.value if (rec := rec_for(r, e)) else "missing")
            for e in r["roster"]}
    name = {e.mention_id: f"{e.name} ({r['key'].get(e.meeting_id)})" for e in r["roster"]}
    people = {pid(r, e.mention_id) for e in r["roster"]}
    anchored = sum(bool(p.officer_ids) for p in r["person"] if p.person_id in people)
    lines = [f"# Evaluation report — {r['run_id']}", "",
             f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC under `config/policy.yaml`.", ""]

    lines += ["## Headline", ""]
    scorer = load_scorer(gold_dir)
    graded = gold_lines = by_flag = None
    if scorer is None:
        lines.append(f"- No gold set in `{gold_dir.name}/`: nothing to grade against.")
    else:
        graded, gold_lines, by_flag = grade(conn, r, policy, scorer, csv_dir or out.parent / "out")
        n, outside = coverage(r, scorer)
        g = graded
        lines += [f"- **Wrong AGAINST votes: {g['wrong_against']}** (target 0)",
                  f"- Wrong FOR votes: {g['wrong_for']}",
                  f"- Vote accuracy: {_pct(g['vote_accuracy'])} on {g['votes_scored']} "
                  f"gold-scored entries; REVIEW rate {_pct(g['review_rate'])}; points interval "
                  f"exact {_pct(g['points_exact'])}",
                  f"- People, pairwise: precision {_pct(g['identity_precision'])}, recall "
                  f"{_pct(g['identity_recall'])}",
                  f"- Countable seats: precision {_pct(g.get('seat_precision'))}, recall "
                  f"{_pct(g.get('seat_recall'))}",
                  f"- Gold covers {n} of {len(r['roster'])} roster entries in this run"
                  + (f"; not in gold: {', '.join(outside)}" if outside else "")]
    lines += [f"- {len(r['roster'])} roster mentions → {len(people)} people; {anchored} anchored "
              f"to Companies House", ""]

    lines += ["## Votes by meeting", "", "| Meeting | Directors | FOR | AGAINST | REVIEW |",
              "| --- | --- | --- | --- | --- |"]
    for mid, key in sorted(r["key"].items(), key=lambda x: x[1]):
        c = Counter(pred[e.mention_id] for e in r["roster"] if e.meeting_id == mid)
        lines.append(f"| {key} | {sum(c.values())} | {c['FOR']} | {c['AGAINST']} | "
                     f"{c['REVIEW']} |")
    c = Counter(pred.values())
    lines += [f"| all | {len(pred)} | {c['FOR']} | {c['AGAINST']} | {c['REVIEW']} |", ""]

    t = policy.ned_points_threshold
    edge = [(e, rec) for e in r["roster"] if (rec := rec_for(r, e))
            and rec.points_lo <= t + 1 and rec.points_hi >= t]
    lines += [f"## At the threshold ({t} points; more is AGAINST for a non-executive)", "",
              "| Director | Points | Vote | Unknowns that would settle it |",
              "| --- | --- | --- | --- |"]
    lines += [f"| {name[e.mention_id]} | {fmt_points(rec)} | {rec.vote.value} | "
              f"{unknowns(r, rec) or '—'} |" for e, rec in edge]
    lines.append("")

    reviews = [(e, rec) for e in r["roster"] if (rec := rec_for(r, e)) and rec.vote == "REVIEW"]
    lines += ["## REVIEW votes", ""] + [
        f"- {name[e.mention_id]}: {fmt_points(rec)} pts; settle "
        f"{unknowns(r, rec) or 'more than one fact together'}" for e, rec in reviews] + [""]

    aim = aim_changes(conn, r)
    lines += ["## Votes that depend on the AIM flag", "",
              f"`aim_counts_as_listed` is `{policy.aim_counts_as_listed}`.", ""]
    lines += (["| Director | unknown | yes | no |", "| --- | --- | --- | --- |"]
              + [f"| {a['name']} ({a['meeting']}) | {a['unknown']} | {a['yes']} | {a['no']} |"
                 for a in aim] if aim else ["None."])
    lines.append("")

    if gold_lines is not None:
        lines += ["## Graded against the gold set", "",
                  f"Policy flags: {gold_flags(policy)}. REVIEW is never counted wrong; it is "
                  "reported as deferred.", ""]
        lines += [("#" + x) if x.startswith("## ") else x for x in gold_lines] + [""]
        lines += ["## Gold-graded under each AIM value", "",
                  "| aim_counts_as_listed | Wrong AGAINST | Wrong FOR | Accuracy | REVIEW rate |",
                  "| --- | --- | --- | --- | --- |"]
        lines += [f"| {f} | {x['wrong_against']} | {x['wrong_for']} | "
                  f"{_pct(x['vote_accuracy'])} | {_pct(x['review_rate'])} |" for f, x in by_flag]
        lines.append("")

    from eval.hard_cases import hard_cases

    hc = hard_cases(conn, r, gold_dir / "hard_cases.yaml")
    tally = Counter(x[2] for x in hc)
    lines += [f"## Hard cases ({tally['PASS']} pass, {tally['FAIL']} fail, "
              f"{tally['DEFERRED']} deferred, {tally['N/A']} n/a)", "",
              "DEFERRED: the run left a fact unknown (a REVIEW vote, a points range, an unknown "
              "fact) instead of getting it wrong; the detail names what would settle it.", "",
              "| Id | Case | Result | Detail |", "| --- | --- | --- | --- |"]
    lines += [f"| {i} | {c} | {res} | {d.replace('|', '/')} |" for i, c, res, d in hc]
    lines.append("")

    sources = Counter(ev.source for rec in r["recommendation"] for ev in rec.evidence)
    researched = sum(any(ev.source == "web" for ev in o.evidence) for o in r["org"])
    lines += ["## Counts", "",
              f"- Seats by status: {dict(Counter(str(s.status) for s in r['seat']))}",
              f"- Review items by kind: {dict(Counter(x.kind for x in r['review']))}",
              f"- Organisations with cited web research: {researched} of {len(r['org'])}",
              f"- Evidence behind the votes, by source: {dict(sources)}", ""]
    text = "\n".join(lines)
    out.write_text(text)
    return text
