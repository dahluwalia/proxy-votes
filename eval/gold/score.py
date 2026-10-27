"""Score pipeline output against the gold set.

Design choice: the scorer reads three small CSVs the pipeline exports (see README), so it does not
depend on the pipeline's database schema. The headline number is wrong AGAINST votes: voting
against the wrong person is the analyst's stated fear. REVIEW is never counted as wrong; it is
reported as a deferral, so a system that defers honestly scores better than one that guesses.

Usage:
  python score.py PRED_DIR [--aim yes|no|unknown] [--chair-designate yes|no|unknown] [--out report.md]
  python score.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import itertools
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build_gold import DEFAULT_POLICY, m_agm, score_person, seat_facts, tri  # noqa: E402  (same policy logic as the gold build)

VOTES = ("FOR", "AGAINST", "REVIEW")


def load_gold():
    g = {n: yaml.safe_load((HERE / f"{n}.yaml").read_text())
         for n in ("roster", "persons", "seats", "votes", "meetings")}
    g["by_key"] = {r["key"]: r for r in g["roster"]}
    g["key_of"] = {r["mention_id"]: r["key"] for r in g["roster"]} | {r["key"]: r["key"] for r in g["roster"]}
    return g


def read_csv(path):
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8") as f:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def norm_org(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\b(the|plc|limited|ltd|llp|llc|inc|n\.?v|s\.?a|ag|oyj|asa|as|pte|group|holdings|company|co)\b\.?", " ", s)
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def org_match(a: str, b: str) -> bool:
    x, y = norm_org(a), norm_org(b)
    return bool(x) and (x == y or SequenceMatcher(None, x, y).ratio() >= 0.88)


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


# ---------------------------------------------------------------- scoring
def score(pred_dir: Path, policy: dict) -> tuple[dict, list[str]]:
    g = load_gold()
    lines, m = [], {}
    roster = read_csv(pred_dir / "roster.csv") or []
    pred_person = {}
    unknown_mentions = []
    for row in roster:
        key = g["key_of"].get(row["mention"])
        if key:
            pred_person[key] = row["person_id"]
        else:
            unknown_mentions.append(row["mention"])

    # 1. roster
    gold_keys = set(g["by_key"])
    found = set(pred_person)
    p, r, _ = prf(len(found), len(unknown_mentions), len(gold_keys - found))
    m["roster_precision"], m["roster_recall"] = p, r
    lines += ["## Roster", f"- recall {r:.1%} ({len(found)}/{len(gold_keys)}), precision {p:.1%}"]
    if gold_keys - found:
        lines.append(f"- missing: {', '.join(sorted(gold_keys - found))}")
    if unknown_mentions:
        lines.append(f"- not on the gold roster: {', '.join(unknown_mentions)}")

    # 2. identity (pairwise over mentions present in the prediction)
    gold_person = {k: g["by_key"][k]["person"] for k in found}
    pairs = list(itertools.combinations(sorted(found), 2))
    gold_same = {pr for pr in pairs if gold_person[pr[0]] == gold_person[pr[1]]}
    pred_same = {pr for pr in pairs if pred_person[pr[0]] == pred_person[pr[1]]}
    p, r, f1 = prf(len(gold_same & pred_same), len(pred_same - gold_same), len(gold_same - pred_same))
    m.update(identity_precision=p, identity_recall=r, identity_f1=f1)
    lines += ["", "## Identity (pairwise)", f"- precision {p:.1%}, recall {r:.1%}, F1 {f1:.3f} "
              f"({len(gold_same)} true pairs, {len(pred_same)} predicted)"]
    for a, b in sorted(pred_same - gold_same):
        lines.append(f"- false merge: {a} + {b}")
    for a, b in sorted(gold_same - pred_same):
        lines.append(f"- missed merge: {a} + {b}")

    # 3. votes
    votes = {g["key_of"].get(v["mention"]): v for v in (read_csv(pred_dir / "votes.csv") or [])}
    conf = Counter()
    issues = defaultdict(list)
    exact_points = scored = 0
    for e in g["votes"]:
        k = e["key"]
        pv = votes.get(k)
        pred = pv["vote"].upper() if pv else "MISSING"
        gold = gold_vote(e, policy)
        if gold is None:
            if pred == "AGAINST":
                issues["AGAINST with no gold (check by hand)"].append(f'{k} {e["director"]}')
            continue
        scored += 1
        conf[(gold, pred)] += 1
        if pred == "AGAINST" and gold != "AGAINST":
            issues["WRONG AGAINST"].append(f'{k} {e["director"]} (gold {gold}: {e["reason"]})')
        elif pred == "FOR" and gold == "AGAINST":
            issues["wrong FOR"].append(f'{k} {e["director"]} ({e["reason"]})')
        elif pred == "REVIEW" and gold != "REVIEW":
            issues["deferred (REVIEW where gold decides)"].append(f'{k} {e["director"]} (gold {gold})')
        elif gold == "REVIEW" and pred in ("FOR", "AGAINST"):
            issues["overconfident (gold is REVIEW)"].append(f'{k} {e["director"]} said {pred}')
        elif pred == "MISSING":
            issues["missing vote"].append(k)
        if pv and pv.get("points_lo", "") != "" and (int(pv["points_lo"]), int(pv.get("points_hi") or pv["points_lo"])) \
                == gold_points(e, policy, g):
            exact_points += 1
    correct = sum(n for (gv, pv_), n in conf.items() if gv == pv_)
    m.update(
        wrong_against=len(issues["WRONG AGAINST"]), wrong_for=len(issues["wrong FOR"]),
        vote_accuracy=correct / scored if scored else 0.0,
        review_rate=sum(n for (_, pv_), n in conf.items() if pv_ == "REVIEW") / scored if scored else 0.0,
        points_exact=exact_points / scored if scored else 0.0, votes_scored=scored,
    )
    lines += ["", "## Votes",
              f"- **wrong AGAINST: {m['wrong_against']}** (target 0) · wrong FOR: {m['wrong_for']}",
              f"- exact vote accuracy {m['vote_accuracy']:.1%} on {scored} scored entries; "
              f"REVIEW rate {m['review_rate']:.1%}; points interval exact {m['points_exact']:.1%}",
              f"- policy: aim_counts_as_listed={policy['aim_counts_as_listed']}, "
              f"chair_designate_counts_as_chair={policy['chair_designate_counts_as_chair']}",
              "", "| gold \\ predicted | " + " | ".join(VOTES + ("MISSING",)) + " |",
              "|---|" + "---|" * 4]
    for gv in VOTES:
        lines.append(f"| {gv} | " + " | ".join(str(conf[(gv, pv_)]) for pv_ in VOTES + ("MISSING",)) + " |")
    for label in ("WRONG AGAINST", "wrong FOR", "overconfident (gold is REVIEW)",
                  "deferred (REVIEW where gold decides)", "missing vote", "AGAINST with no gold (check by hand)"):
        if issues[label]:
            lines += ["", f"**{label}**"] + [f"- {x}" for x in issues[label]]

    # 4. seats (optional)
    seats = read_csv(pred_dir / "seats.csv")
    if seats is not None:
        lines += [""] + score_seats(g, seats, pred_person, policy, m)
    return m, lines


def gold_points(e, policy, g):
    """Gold's points interval for this entry under `policy` (the stored one is under the
    neutral default flags), recomputed with the gold build's own scoring."""
    r = g["by_key"][e["key"]]
    seats = [s for s in g["seats"] if s["person"] == e["person"]]
    issuer = next((s["org_id"] for s in seats if s["seat_id"] == r.get("issuer_seat_id")), None)
    meeting = next(m for m in g["meetings"] if m["meeting_id"] == e["meeting_id"])
    res = score_person(seats, e["role_at_meeting"], dict(DEFAULT_POLICY, **policy), m_agm(meeting),
                       issuer)
    return res["points_lo"], res["points_hi"]


def gold_vote(e, policy):
    if e["expected_vote"] is None:
        return None
    bp = e.get("by_policy", {})
    for flag, table in bp.items():
        if policy[flag] != "unknown":
            return table[policy[flag]]
    return e["expected_vote"]


def score_seats(g, seats, pred_person, policy, m):
    # map pipeline person ids -> gold person by majority over shared mentions
    votes_for = defaultdict(Counter)
    for k, pid in pred_person.items():
        votes_for[pid][g["by_key"][k]["person"]] += 1
    to_gold = {pid: c.most_common(1)[0][0] for pid, c in votes_for.items()}
    gold_by_person = defaultdict(list)
    for s in g["seats"]:
        gold_by_person[s["person"]].append((s, seat_facts(s, policy)))
    standing = {r["person"] for r in g["roster"]}
    gold_counted = {(s["person"], s["seat_id"]) for p in standing for s, f in gold_by_person[p] if f["counted"] == "yes"}

    matched_gold, tp, errs = set(), 0, defaultdict(list)
    pred_counted = [s for s in seats if tri(s.get("counted")) == "yes"]
    for s in pred_counted:
        person = to_gold.get(s["person_id"])
        cands = [(gs, f) for gs, f in gold_by_person.get(person, []) if org_match(s["org"], gs["org"])]
        # same name first, then a gold seat not yet matched: "Lloyds Bank plc" must not take
        # the "Lloyds Banking Group Plc" seat (a person's seat listed in two notices may repeat)
        cands.sort(key=lambda c: (norm_org(c[0]["org"]) != norm_org(s["org"]),
                                  (person, c[0]["seat_id"]) in matched_gold))
        hit = next(((gs, f) for gs, f in cands if f["counted"] == "yes"), None)
        if hit:
            tp += 1
            matched_gold.add((person, hit[0]["seat_id"]))
            if tri(s.get("chair")) != hit[1]["chair"] and hit[1]["chair"] != "unknown":
                errs["chair wrong"].append(f'{person}: {hit[0]["org"]}')
            continue
        why = "seat not in gold"
        if cands:
            gs, f = cands[0]
            why = ("past role counted as current" if f["current"] == "no" else
                   "non-board role counted" if gs["board_seat"] == "no" else
                   "unlisted organisation counted" if f["listed"] == "no" else
                   "counted where gold is undecided")
        errs[why].append(f'{person or s["person_id"]}: {s["org"]}')
    p, r, f1 = prf(tp, len(pred_counted) - tp, len(gold_counted - matched_gold))
    m.update(seat_precision=p, seat_recall=r)
    out = ["## Countable seats", f"- precision {p:.1%}, recall {r:.1%} ({len(gold_counted)} gold countable seats)"]
    for why, items in errs.items():
        out += [f"- {why}: {len(items)}"] + [f"    - {x}" for x in items]
    missed = sorted(gold_counted - matched_gold)
    if missed:
        names = {s["seat_id"]: s["org"] for s in g["seats"]}
        out += [f"- missed: {len(missed)}"] + [f"    - {p_}: {names[sid]}" for p_, sid in missed]
    return out


# ---------------------------------------------------------------- self-test
def write_perfect(dirpath: Path):
    g = load_gold()
    with (dirpath / "roster.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mention", "person_id"])
        for r in g["roster"]:
            w.writerow([r["mention_id"], "P-" + r["person"]])
    with (dirpath / "votes.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mention", "vote", "points_lo", "points_hi"])
        for e in g["votes"]:
            vote = e["expected_vote"] or e.get("vote_on_disclosed_data") or "REVIEW"  # withdrawn: any non-AGAINST
            w.writerow([e["key"], vote, e["points_lo"], e["points_hi"]])
    with (dirpath / "seats.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["person_id", "org", "current", "board_seat", "chair", "listed", "counted"])
        for s in g["seats"]:
            w.writerow(["P-" + s["person"], s["org"], s["tense"], s["board_seat"], s["chair"], s["listed"], s["counted"]])


def selftest():
    default = {"aim_counts_as_listed": "unknown", "chair_designate_counts_as_chair": "unknown",
               "debt_only_listing_counts": "unknown"}
    tmp = Path(tempfile.mkdtemp())
    try:
        write_perfect(tmp)
        m, _ = score(tmp, default)
        assert m["wrong_against"] == 0 and m["vote_accuracy"] == 1.0, m
        assert m["identity_f1"] == 1.0 and m["roster_recall"] == 1.0, m
        assert m["seat_precision"] == 1.0 and m["seat_recall"] == 1.0, m

        # inject the mistakes the analyst fears most
        rows = list(csv.reader((tmp / "votes.csv").open()))
        for row in rows:
            if row[0] == "Tesco#10":
                row[1] = "AGAINST"          # Gerry Murphy at 4 points
            if row[0] == "Severn Trent#7":
                row[1] = "REVIEW"           # Helen Miles deferred
        csv.writer((tmp / "votes.csv").open("w", newline="")).writerows(rows)
        rows = list(csv.reader((tmp / "roster.csv").open()))
        for row in rows:
            if row[0] == "nm:04967001:r12":
                row[1] = "P-someone-else"   # split Chris Kennedy's ITV mention off
        csv.writer((tmp / "roster.csv").open("w", newline="")).writerows(rows)
        with (tmp / "seats.csv").open("a", newline="") as f:
            csv.writer(f).writerow(["P-christine-hodgson", "Standard Chartered PLC", "current", "yes", "no", "yes", "yes"])
        m, lines = score(tmp, default)
        text = "\n".join(lines)
        assert m["wrong_against"] == 1 and "Tesco#10 Gerry Murphy" in text, text
        assert "deferred" in text and m["identity_recall"] < 1.0, text
        assert "past role counted as current: 1" in text, text
        m, _ = score(tmp, dict(default, aim_counts_as_listed="yes"))
        assert m["vote_accuracy"] < 1.0  # Gilliland's gold flips to AGAINST under aim=yes
        print("selftest passed")
    finally:
        shutil.rmtree(tmp)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pred_dir", nargs="?", type=Path)
    ap.add_argument("--aim", default="unknown", choices=("yes", "no", "unknown"))
    ap.add_argument("--chair-designate", default="unknown", choices=("yes", "no", "unknown"))
    ap.add_argument("--debt-only", default="unknown", choices=("yes", "no", "unknown"),
                    help="debt_only_listing_counts: do bond-only listings (banks, finance companies) count")
    ap.add_argument("--out", type=Path, help="write the report as markdown")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.pred_dir:
        ap.error("pred_dir is required")
    policy = {"aim_counts_as_listed": a.aim, "chair_designate_counts_as_chair": a.chair_designate,
              "debt_only_listing_counts": a.debt_only}
    _, lines = score(a.pred_dir, policy)
    report = "# Gold-set evaluation\n\n" + "\n".join(lines) + "\n"
    print(report)
    if a.out:
        a.out.write_text(report)


if __name__ == "__main__":
    main()
