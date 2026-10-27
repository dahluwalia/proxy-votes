"""The newest analyst decision wins: what a new merge or split replaces."""

from datetime import UTC, datetime

from pennant.core.models import Decision
from pennant.state.supersede import replaced_by


def d(n, kind, a, b):
    return Decision(decision_id=f"dec:{n}" if n else "(new)", kind=kind,
                    payload={"a": a, "b": b}, author="analyst", reason="r",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC))


def ids(ds):
    return [x.decision_id for x in ds]


def test_split_replaces_the_merge_it_contradicts():
    assert ids(replaced_by([d(1, "must_link", "a", "b")],
                           d(0, "cannot_link", ["a"], ["b"]))) == ["dec:1"]


def test_split_undoes_only_the_merge_on_the_chain_between_the_sides():
    active = [d(1, "must_link", "a", "b"), d(2, "must_link", "b", "c"),
              d(3, "must_link", "c", "e")]
    assert ids(replaced_by(active, d(0, "cannot_link", ["a"], ["b"]))) == ["dec:1"]
    # a and c are joined through b by dec:1 and dec:2: the newer one goes first, enough
    assert ids(replaced_by(active, d(0, "cannot_link", ["a"], ["c"]))) == ["dec:2"]


def test_split_with_two_chains_undoes_one_merge_on_each():
    active = [d(1, "must_link", "a", "b"), d(2, "must_link", "a", "c"),
              d(3, "must_link", "c", "b")]  # a–b directly, and a–c–b
    assert sorted(ids(replaced_by(active, d(0, "cannot_link", ["a"], ["b"])))) == \
        ["dec:1", "dec:3"]


def test_merge_replaces_the_split_keeping_the_two_apart():
    active = [d(1, "cannot_link", ["a"], ["b"]), d(2, "cannot_link", ["x"], ["y"])]
    assert ids(replaced_by(active, d(0, "must_link", "a", "b"))) == ["dec:1"]


def test_nothing_to_replace_when_there_is_no_contradiction():
    active = [d(1, "must_link", "a", "b"), d(2, "cannot_link", ["a"], ["z"])]
    assert replaced_by(active, d(0, "must_link", "b", "c")) == []
    assert replaced_by(active, d(0, "cannot_link", ["c"], ["d"])) == []
