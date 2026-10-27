"""The bundled nickname list: pairs in either direction, never chained."""

from pennant.dedup.nicknames import is_nickname, pairs


def test_listed_pairs_match_either_way():
    assert is_nickname("tom", "thomas") and is_nickname("thomas", "tom")
    assert is_nickname("jez", "jeremy") and is_nickname("bob", "robert")


def test_pairs_are_never_chained_or_self_matches():
    # Bill is listed for both Robert and William; chaining would make Robert a William.
    assert is_nickname("robert", "bill") and is_nickname("bill", "william")
    assert not is_nickname("robert", "william")
    assert not is_nickname("tom", "tom") and not is_nickname("ken", "gerard")


def test_the_list_loads_in_full():
    assert len(pairs()) > 2500
