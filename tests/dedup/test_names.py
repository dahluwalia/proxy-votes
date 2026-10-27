"""M4.1: name parsing and comparison (hard cases H1-H3)."""

import itertools

import pytest

from pennant.dedup.names import LEVELS, at_least, compare, fold, parse

# --- H1: Ken Murphy and Gerry Murphy on the same Tesco board ------------------


def test_h1_same_surname_different_first_names_are_variants_for_the_ranker():
    """No dictionary: compare() can't tell Gerry=Gerard from Ken!=Gerard. Both are
    variants; the TF-IDF ranker in anchor.py separates them (test_anchor H1)."""
    for a, b in [("Ken Murphy", "MURPHY, Kenneth"), ("Dr Gerry Murphy", "MURPHY, Gerard"),
                 ("Ken Murphy", "MURPHY, Gerard"), ("Ken Murphy", "Dr Gerry Murphy")]:
        assert compare(a, b) == "variant"
    assert compare("Ken Murphy", "MURPHY, Ken") == "exact"


# --- H2: titles and postnominals are stripped before comparing ----------------


@pytest.mark.parametrize(
    ("notice", "ch", "level"),
    [
        ("Dame Carolyn Fairbairn DBE", "FAIRBAIRN, Carolyn Julie", "exact"),
        ("Dr Gerry Murphy", "MURPHY, Gerard", "variant"),
        ("Christine Hodgson CBE BSc (Hons) FCA", "HODGSON, Christine Mary", "exact"),
        ("Christine Hodgson CBE, BSc (Hons), FCA", "HODGSON, Christine", "exact"),
        ("Sharmila Nebhrajani OBE", "NEBHRAJANI, Sharmila", "exact"),
        ("Tom Delay CBE", "DELAY, Thomas", "variant"),
        ("Sir Edward Smith", "SMITH, Edward, Sir", "exact"),
        ("Gerry Murphy", "MURPHY, Dr Gerard", "variant"),
    ],
)
def test_h2_titles_and_postnominals(notice, ch, level):
    assert compare(notice, ch) == level


def test_h2_parse_strips():
    p = parse("Christine Hodgson CBE BSc (Hons) FCA")
    assert p.tokens == ("christine", "hodgson")
    assert parse("Dame Carolyn Fairbairn DBE").tokens == ("carolyn", "fairbairn")
    assert parse("Dr Gerry Murphy").given == ("gerry",)


# --- H3: name structure -------------------------------------------------------


@pytest.mark.parametrize(
    ("notice", "ch"),
    [
        ("Edward Bonham Carter", "BONHAM CARTER, Edward"),
        ("Edward Bonham Carter", "BONHAM CARTER, Edward Henry"),
        ("Edward Bonham Carter", "BONHAM-CARTER, Edward"),
        ("Edward Bonham Carter", "Edward Henry BONHAM CARTER"),
        ("Hilde Merete Aasheim", "AASHEIM, Hilde Merete"),
        ("Hilde Merete Aasheim", "Hilde Merete AASHEIM"),
        ("Jean-Michel Valette", "VALETTE, Jean-Michel"),
        ("Jean-Michel Valette", "VALETTE, Jean Michel"),
        ("André Lacroix", "LACROIX, Andre"),
        ("André Lacroix", "LACROIX, André Rene"),
        ("Carolyn McCall", "MCCALL, Carolyn Julie"),
    ],
)
def test_h3_exact_match_to_ch_record(notice, ch):
    assert compare(notice, ch) == "exact"


def test_h3_parse_ch_formats():
    p = parse("BONHAM CARTER, Edward Henry")
    assert p.given == ("edward", "henry") and p.surname == "bonham carter"
    q = parse("Edward Henry BONHAM CARTER")
    assert q.given == ("edward", "henry") and q.surname == "bonham carter"
    # Plain notice names: the surname is unknown, so every suffix is a candidate.
    assert parse("Edward Bonham Carter").surname_len is None
    assert len(parse("Edward Bonham Carter").splits()) == 2


# --- other levels -------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "level"),
    [
        ("Chris Kennedy", "KENNEDY, Christopher John", "variant"),
        ("Chris Kennedy", "Christopher John KENNEDY", "variant"),
        ("Christopher Kennedy", "KENNEDY, Christopher John", "exact"),
        ("Archie Norman", "NORMAN, Archibald John", "variant"),
        ("Jez Maiden", "MAIDEN, Jeremy", "variant"),
        ("Rob Smith", "Bob Smith", "variant"),
        ("C Kennedy", "KENNEDY, Christopher", "initial"),
        ("C. J. Kennedy", "KENNEDY, Christopher John", "initial"),
        ("Christopher J Kennedy", "KENNEDY, Christopher John", "exact"),
        ("Murphy", "MURPHY, Kenneth", "surname_only"),
        # different full first names are variants; the ranker scores them
        ("Christopher Kennedy", "KENNEDY, Christine", "variant"),
        ("Stephen Mogford", "MOGFORD, Steven", "variant"),
        # middle names and initials still veto
        ("Christopher John Kennedy", "KENNEDY, Christopher Paul", "conflict"),
        ("C Kennedy", "KENNEDY, Mark", "conflict"),
        ("Ken Murphy", "Caroline Silver", "conflict"),
        # known by a later forename: a variant, never exact
        ("Andrew Smith", "SMITH, John Andrew", "variant"),
        ("Nick Hampton", "HAMPTON, Anthony Nicholas Seymour", "variant"),
        ("Nick Hampton", "Anthony Nicholas Seymour HAMPTON", "variant"),
        ("N Hampton", "HAMPTON, Anthony Nicholas", "conflict"),  # initials: first name only
        ("", "SMITH, John", "conflict"),
    ],
)
def test_levels(a, b, level):
    assert compare(a, b) == level


NAMES = [
    "Ken Murphy", "MURPHY, Kenneth", "Dr Gerry Murphy", "MURPHY, Gerard", "Chris Kennedy",
    "KENNEDY, Christopher John", "C Kennedy", "Edward Bonham Carter", "BONHAM CARTER, Edward",
    "Murphy", "Andrew Smith", "SMITH, John Andrew", "Christine Hodgson CBE BSc (Hons) FCA",
]


def test_compare_is_symmetric():
    for a, b in itertools.permutations(NAMES, 2):
        assert compare(a, b) == compare(b, a), (a, b)


def test_compare_accepts_parsed_names():
    assert compare(parse("Ken Murphy"), "MURPHY, Ken") == "exact"


def test_at_least_and_fold():
    assert at_least("exact", "variant") and at_least("variant", "variant")
    assert not at_least("initial", "variant")
    assert [at_least(lvl, "conflict") for lvl in LEVELS] == [True] * 5
    assert fold("André LACROIX") == "andre lacroix"


def test_letters_without_a_base_letter_are_spelled_out_not_dropped():
    assert fold("Søren SØRENSEN") == "soren sorensen"
    assert fold("Łukasz Æbelø") == "lukasz aebelo"
    assert compare("Søren Sørensen", "Soren SORENSEN") == "exact"


def test_real_roster_only_h4_pairs_match_by_name(ctx):
    """On the 50 extracted mentions, the only exact matches are the three H4 people.
    Ken and Gerry Murphy share a surname, so they are a variant pair for the ranker."""
    from pennant.core import db
    from pennant.extract import validate

    assert validate.run(ctx).ok
    roster = db.get_outputs(ctx.conn, ctx.run_id, "roster")
    assert len(roster) == 50
    matches = {
        (a.mention_id, b.mention_id): compare(a.name, b.name)
        for a, b in itertools.combinations(roster, 2)
        if compare(a.name, b.name) != "conflict"
    }
    assert matches == {
        ("nm:00445790:r9", "nm:04967001:r12"): "exact",  # Chris Kennedy, Tesco and ITV
        ("nm:02366619:r11", "nm:04967001:r14"): "exact",  # Sharmila Nebhrajani (OBE)
        ("nm:04256886:r10", "nm:04267576:r10"): "exact",  # Tamara Ingram, M&S and Intertek
        ("nm:00445790:r10", "nm:00445790:r11"): "variant",  # Gerry and Ken Murphy, Tesco
    }


# --- honours in the surname part of a CH name (review fix) --------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("NEBHRAJANI OBE, Sharmila", "Sharmila NEBHRAJANI"),  # was a conflict
        ("MCCALL DBE, Carolyn Julia", "MCCALL, Carolyn Julia, Dame"),
        ("Sharmila NEBHRAJANI  OBE", "NEBHRAJANI, Sharmila"),
        ("MA, Jack", "Jack Ma"),  # a surname that is also a degree is kept when alone
        ("Jane Smith FRICS FCIS FRSE FRCP FIET FCMI FInstD", "SMITH, Jane"),
    ],
)
def test_honours_in_a_ch_surname_are_stripped(a, b):
    assert compare(a, b) == "exact"
