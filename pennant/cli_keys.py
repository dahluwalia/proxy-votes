"""Plain-language keys for the CLI, so every screen explains its own codes.

Design choice: one place holds what each status, coverage, review kind, fact and rule
means, and `show`, `votes` and `review` print only the entries their output uses. The
wording speaks to an analyst who knows the policy, not the code.
"""

from __future__ import annotations

import re

from pennant.core.models import Recommendation

VOTE_KEY = ("FOR: neither rule applies · AGAINST: a rule applies · REVIEW: an unknown fact "
            "decides whether a rule applies")
POINTS_KEY = ("Points: 1 per counted board seat, 2 if it is a chair; a range like 0–1 means a "
              "fact about that seat is unknown; seats in one corporate group count once")
RULES_KEY = ("Points rule: a non-executive here with more than {limit} points · Executive "
             "rule: an executive job at a listed company plus a counted board seat in "
             "another group")
TRI_KEY = "✓ yes   ✗ no   ? unknown"

COVERAGE = {
    "structured": "the notice lists current appointments",
    "prose": "the notice has a biography but no appointments list, so roles may be missed",
    "none": "the notice has no biography, so outside roles come from Companies House only",
}

STATUS = {
    "confirmed": "the notice and Companies House agree it was held on the AGM date",
    "stale": "the notice lists it, but Companies House shows it ended before the AGM",
    "contradiction": "the notice lists it, but Companies House has no such appointment",
    "undisclosed": "Companies House shows it on the AGM date; the notice leaves it out",
    "ch_invisible": "not on Companies House (not a UK company); facts come from the notice "
                    "and web research",
    "ch_unavailable": "Companies House couldn't be reached, so it is unchecked",
    "past": "a former role, so it doesn't count",
    "unreconciled": "not yet checked against Companies House",
}

SEAT_COLUMNS = ("Board: a seat on the board · Chair: chairs the board · Exec: an executive "
                "job · Current: held on the AGM date · Listed: shares (or bonds) listed on the "
                "AGM date · ID: the record is this person. A seat counts when Board, Current, "
                "Listed and ID are all ✓")

FACT_QUESTION = {
    "board_seat": "is it a seat on the board?",
    "chair": "is the role chair of the board?",
    "executive": "is it an executive job?",
    "current": "was it held on the AGM date?",
    "listed": "was the organisation listed on the AGM date?",
    "identity": "is this record the same person?",
    "role_type_at_issuer": "is the director a non-executive here? (the notice states no role)",
    "ch_record": "does the person hold directorships the notice leaves out? (no Companies "
                 "House record could be read)",
}

REVIEW = {
    "not_in_office": "matched to a Companies House record, but not a director of this "
                     "company on the AGM date",
    "stale": "the notice lists a seat that Companies House shows ended before the AGM",
    "contradiction": "the notice lists a seat Companies House has no record of",
    "undisclosed": "Companies House shows a directorship the notice leaves out",
    "ch_unavailable": "Companies House couldn't be reached; affected facts are unknown",
    "unanchored": "no Companies House record matched this name, so outside seats are unknown",
    "possible_same_person": "two Companies House records match about equally; pick one",
    "no_dob": "a Companies House record has no birth date, so it can't be linked safely",
    "search_incomplete": "the Companies House name search stopped early; some records may be "
                         "missing",
    "link_refused": "a match was not applied because it would join people kept apart",
    "decision_conflict": "an analyst decision could not be applied; it clashes with another",
    "decision_stale": "an analyst decision names someone no longer in the notices",
    "ungrounded_quote": "an appointment's quote is not in the PDF, so it was dropped",
    "ungrounded_role": "the role quoted for this company is not in the PDF; treated as unstated",
    "extract_error": "a notice could not be read",
    "extract_warning": "something in a notice looked off while reading it",
    "web_research_failed": "web research errored; the facts it would answer stay unknown",
    "web_unsourced": "a web answer cited no page the search returned, so it was dropped",
    "site_not_found": "the firm's own website wasn't found, so roles there stay unknown",
    "stale_evidence": "a source was last checked before the AGM; facts from it may be out of "
                      "date",
    "score_error": "scoring failed, so the vote is REVIEW",
}


def tri_word(v: object, yes: str = "applies", no: str = "doesn't apply",
             unknown: str = "might apply") -> str:
    return {"yes": yes, "no": no}.get(str(v), unknown)


def rule_lines(rec: Recommendation) -> list[str]:
    """The two rules in words, with the numbers this vote used."""
    m = re.search(r"> (\d+): \w+; non-executive here: (\w+)", " ".join(rec.reasons))
    limit, ned = (m.group(1), m.group(2)) if m else ("4", "unknown")
    pts = f"{rec.points_lo}" if rec.points_lo == rec.points_hi else \
        f"{rec.points_lo}–{rec.points_hi}"
    here = {"yes": "a non-executive here", "no": "an executive here, so it can't apply"}.get(
        ned, "role here not stated")
    return [
        f"Points rule {tri_word(rec.points_rule)}: {pts} points against a limit of {limit} "
        f"({here})",
        f"Executive rule {tri_word(rec.exec_rule)}: against an executive at a listed company "
        "who also holds a counted board seat in another group",
    ]


def why_line(rec: Recommendation) -> str:
    if rec.vote == "AGAINST":
        rules = [n for n, v in (("points rule", rec.points_rule),
                                ("executive rule", rec.exec_rule)) if str(v) == "yes"]
        return f"AGAINST because the {' and the '.join(rules) or 'policy'} applies"
    if rec.vote == "FOR":
        return "FOR because neither rule applies"
    n = len(rec.unknown_facts)
    return ("REVIEW because a rule might apply: " +
            (f"settling the {'fact' if n == 1 else f'{n} facts'} below decides the vote"
             if n else "see the seats below"))


def is_summary_reason(reason: str) -> bool:
    """Score's own one-line rule summaries, which rule_lines() replaces."""
    return reason.startswith(("points ", "executive rule:"))
