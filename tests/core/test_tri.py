"""M0.1: Kleene truth tables, intervals and seat points."""

import pytest

from pennant.core.tri import (
    Interval,
    imax,
    isum,
    seat_points,
    t_and,
    t_not,
    t_or,
    to_tri,
)

Y, N, U = "yes", "no", "unknown"

AND_TABLE = {
    (Y, Y): Y, (Y, U): U, (Y, N): N,
    (U, Y): U, (U, U): U, (U, N): N,
    (N, Y): N, (N, U): N, (N, N): N,
}
OR_TABLE = {
    (Y, Y): Y, (Y, U): Y, (Y, N): Y,
    (U, Y): Y, (U, U): U, (U, N): U,
    (N, Y): Y, (N, U): U, (N, N): N,
}


@pytest.mark.parametrize(("a", "b"), list(AND_TABLE))
def test_t_and_truth_table(a, b):
    assert t_and(a, b) == AND_TABLE[(a, b)]


@pytest.mark.parametrize(("a", "b"), list(OR_TABLE))
def test_t_or_truth_table(a, b):
    assert t_or(a, b) == OR_TABLE[(a, b)]


def test_t_not():
    assert (t_not(Y), t_not(N), t_not(U)) == (N, Y, U)


def test_variadic_and_identities():
    assert t_and(Y, Y, U, Y) == U
    assert t_and(Y, U, N) == N
    assert t_or(N, N, U) == U
    assert t_and() == Y and t_or() == N


def test_interval_gt():
    assert Interval(4, 4).gt(4) == "no"
    assert Interval(4, 5).gt(4) == "unknown"
    assert Interval(5, 6).gt(4) == "yes"


def test_interval_add_and_imax():
    assert Interval(1, 2) + Interval(0, 1) == Interval(1, 3)
    assert Interval(1, 1).imax(Interval(0, 2)) == Interval(1, 2)
    assert isum([Interval(1, 1), Interval(0, 2), Interval(2, 2)]) == Interval(3, 5)
    assert imax([Interval(1, 1), Interval(0, 2)]) == Interval(1, 2)
    with pytest.raises(ValueError):
        Interval(2, 1)


SEAT_POINTS = {
    (Y, Y): (2, 2), (Y, N): (1, 1), (Y, U): (1, 2),
    (N, Y): (0, 0), (N, N): (0, 0), (N, U): (0, 0),
    (U, Y): (0, 2), (U, N): (0, 1), (U, U): (0, 2),
}


@pytest.mark.parametrize(("counted", "chair"), list(SEAT_POINTS))
def test_seat_points_all_nine(counted, chair):
    assert seat_points(counted, chair) == Interval(*SEAT_POINTS[(counted, chair)])


def test_to_tri():
    assert to_tri(True) == Y and to_tri(False) == N and to_tri(None) == U
    assert to_tri(" YES ") == Y and to_tri("Unknown") == U
    with pytest.raises(ValueError):
        to_tri("maybe")
