"""Known nicknames, so a familiar first name in a notice anchors to the formal one on CH.

Design choice: character 3-grams can't see that "Tom" is "Thomas" (they share little but
the first letter), so a notice's "Tom Delay" never cleared the ranker's score. The
carltonnorthern/nicknames list (Apache 2.0, pinned in data/README.md) supplies the pairs.
A pair counts only as listed, in either direction ("thomas has nickname tom"), never
chained: gerry is a nickname of both gerald and jerry, and chaining would make gerald a
jerry. A nickname match scores like an exact first name in the ranker, but the anchor
still needs its margin over the issuer's other same-surname directors.
"""

from __future__ import annotations

import csv
from functools import cache
from pathlib import Path

DATA = Path(__file__).with_name("data") / "nicknames.csv"


@cache
def pairs() -> frozenset[frozenset[str]]:
    """Every listed {name, nickname} pair, folded to lower case."""
    with DATA.open(newline="", encoding="utf-8") as f:
        rows = csv.DictReader(f)
        return frozenset(frozenset((r["name1"].strip().casefold(), r["name2"].strip().casefold()))
                         for r in rows if r["relationship"] == "has_nickname")


def is_nickname(a: str, b: str) -> bool:
    """True when one folded first name is a listed nickname of the other."""
    return a != b and frozenset((a, b)) in pairs()
