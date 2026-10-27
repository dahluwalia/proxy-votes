"""Three-valued facts and points intervals.

Design choice: every fact is "yes" / "no" / "unknown" (Kleene logic) until the
final vote, and points are an interval [lo, hi] rather than a number. A vote is
decided only when the whole interval agrees; otherwise it is REVIEW. Tri values
are plain strings so they serialise to JSON and compare as `x == "no"`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator

Tri = Literal["yes", "no", "unknown"]
YES: Tri = "yes"
NO: Tri = "no"
UNKNOWN: Tri = "unknown"
TRI_VALUES: tuple[Tri, ...] = (YES, NO, UNKNOWN)


def to_tri(value: Any) -> Tri:
    """Coerce a loose value to Tri.

    bool -> yes/no (PyYAML reads a bare `yes` as True); None -> unknown;
    strings are case- and space-insensitive. Anything else raises ValueError.
    """
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return YES if value else NO
    if isinstance(value, str):
        v = value.strip().lower()
        if v in TRI_VALUES:
            return v  # type: ignore[return-value]
        if v in ("true", "on"):
            return YES
        if v in ("false", "off"):
            return NO
    raise ValueError(f"not a tri value: {value!r}")


# Use in pydantic models: accepts bools and None, stores the canonical string.
TriField = Annotated[Tri, BeforeValidator(to_tri)]


def t_and(*xs: Tri) -> Tri:
    """Kleene AND: no if any is no; yes if all are yes (or none given); else unknown."""
    if NO in xs:
        return NO
    if all(x == YES for x in xs):
        return YES
    return UNKNOWN


def t_or(*xs: Tri) -> Tri:
    """Kleene OR: yes if any is yes; no if all are no (or none given); else unknown."""
    if YES in xs:
        return YES
    if all(x == NO for x in xs):
        return NO
    return UNKNOWN


def t_not(x: Tri) -> Tri:
    return {YES: NO, NO: YES}.get(x, UNKNOWN)  # type: ignore[return-value]


@dataclass(frozen=True)
class Interval:
    """Closed integer interval of possible points."""

    lo: int
    hi: int

    def __post_init__(self) -> None:
        if self.lo > self.hi:
            raise ValueError(f"Interval lo > hi: {self.lo} > {self.hi}")

    def __add__(self, other: Interval) -> Interval:
        return Interval(self.lo + other.lo, self.hi + other.hi)

    def imax(self, other: Interval) -> Interval:
        """Pointwise max: the interval of max(a, b) for a in self, b in other."""
        return Interval(max(self.lo, other.lo), max(self.hi, other.hi))

    def gt(self, n: int) -> Tri:
        """Is the true value > n? yes if lo > n, no if hi <= n, else unknown."""
        if self.lo > n:
            return YES
        if self.hi <= n:
            return NO
        return UNKNOWN


ZERO = Interval(0, 0)


def isum(intervals: Iterable[Interval]) -> Interval:
    total = ZERO
    for iv in intervals:
        total = total + iv
    return total


def imax(intervals: Iterable[Interval]) -> Interval:
    """Max over several intervals; ZERO for none."""
    out = ZERO
    for iv in intervals:
        out = out.imax(iv)
    return out


def seat_points(counted: Tri, chair: Tri) -> Interval:
    """Points one seat can contribute: a counted board seat is 1, a chair 2."""
    lo = 0 if counted != YES else (2 if chair == YES else 1)
    hi = 0 if counted == NO else (1 if chair == NO else 2)
    return Interval(lo, hi)
