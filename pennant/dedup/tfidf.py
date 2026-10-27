"""Character 3-gram TF-IDF over names, scored the way Sparkly scores (its `3gram`
analyzer), in-process.

Design choice: Sparkly runs TF-IDF on Spark and Lucene to find top-k candidates in large
tables. Here the candidates are one issuer's directors in office, about a dozen, so a few
lines of Python do the same job with no JVM. It ranks; it never decides alone. Character
overlap can't tell a nickname from a different name (andre/andrew scores above
tom/thomas), so callers rank only names that already share a surname, and accept the
top one only with a clear score and margin.

Each token is padded ('##ken#') so short given names still get several grams; IDF comes
from the candidates themselves, so a gram every candidate shares counts for little.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable

N = 3


def grams(token: str) -> Counter[str]:
    """'ken' -> {'##k', '#ke', 'ken', 'en#'}."""
    t = "#" * (N - 1) + token + "#"
    return Counter(t[i:i + N] for i in range(len(t) - N + 1))


def idf(corpus: Iterable[str]) -> dict[str, float]:
    """Smoothed inverse document frequency of each gram; one token is one document."""
    docs = [set(grams(t)) for t in corpus]
    df = Counter(g for d in docs for g in d)
    return {g: math.log((1 + len(docs)) / (1 + n)) + 1 for g, n in df.items()}


def similarity(a: str, b: str, weights: dict[str, float]) -> float:
    """Cosine of the two tokens' TF-IDF vectors, 0..1. A gram outside the corpus gets
    the highest weight, as the rarest gram would."""
    top = max(weights.values(), default=1.0)
    va = {g: n * weights.get(g, top) for g, n in grams(a).items()}
    vb = {g: n * weights.get(g, top) for g, n in grams(b).items()}
    dot = sum(v * vb.get(g, 0.0) for g, v in va.items())
    norm = math.sqrt(sum(v * v for v in va.values())) * math.sqrt(sum(v * v for v in vb.values()))
    return dot / norm if norm else 0.0


def best_match(query: str, names: Iterable[str], weights: dict[str, float]) -> float:
    """The query's score against a person's given names: the best single forename, so
    someone known by a middle name ("Nick" for Anthony Nicholas) still scores."""
    return max((similarity(query, n, weights) for n in names), default=0.0)
