"""M2.1: grounding quotes against PyMuPDF page text."""

import json

import pytest

from pennant.core.config import NOTICES_DIR, notices
from pennant.evidence.grounding import (
    PAGES_FIXTURES,
    ground,
    ground_near,
    load_pages,
    normalize,
    page_texts,
)

# page counts of the example notices; a notice added to data/notices needs only a fixture
PAGE_COUNTS = {"itv": 12, "intertek": 12, "marks-and-spencer": 11, "severn-trent": 20,
               "tesco": 16}
NOTICES = notices()


def check_fixture(key, found, fixtures=PAGES_FIXTURES):
    path = fixtures / found[key].replace(".pdf", ".json")
    assert path.exists(), f"no page fixture for {found[key]}: python -m pennant.evidence.grounding"
    data = json.loads(path.read_text())
    assert data["doc"] == found[key]
    assert data["pages"] and all(isinstance(p, str) for p in data["pages"])
    if key in PAGE_COUNTS:
        assert len(data["pages"]) == PAGE_COUNTS[key]
    assert load_pages(key, fixtures) == load_pages(found[key], fixtures) == data["pages"]


@pytest.mark.parametrize("key", sorted(NOTICES))
def test_fixtures_exist_for_every_notice(key):
    check_fixture(key, NOTICES)


def test_a_new_notice_needs_a_fixture_but_no_page_count(tmp_path):
    name = "Acme_Holdings_2026_Notice_of_AGM.pdf"
    (tmp_path / name).write_bytes(b"")
    found = notices(tmp_path)
    assert found == {"acme-holdings": name}
    fixtures = tmp_path / "pages"
    fixtures.mkdir()
    with pytest.raises(AssertionError, match="no page fixture"):
        check_fixture("acme-holdings", found, fixtures)
    (fixtures / name.replace(".pdf", ".json")).write_text(
        json.dumps({"doc": name, "pages": ["Acme Holdings plc"]}))
    check_fixture("acme-holdings", found, fixtures)  # no KeyError on PAGE_COUNTS


def test_fixture_matches_pymupdf():
    assert page_texts(NOTICES_DIR / NOTICES["tesco"]) == load_pages("tesco")


def test_exact_quote_grounds():
    page = load_pages("tesco")[9]
    assert ground(page, "Chair: Barratt Redrow plc and ICE Clear Europe") == (True, "exact")


def test_quote_across_a_line_break_grounds():
    page = load_pages("tesco")[9]
    assert "Tritax Big Box \nREIT plc" in page  # the PDF breaks the line here
    quote = "Senior independent director: The Rank Group plc and Tritax Big Box REIT plc"
    assert ground(page, quote) == (True, "normalized")


def test_quote_across_a_line_break_with_curly_quotes_and_case():
    page = load_pages("severn-trent")[10]
    quote = ("CHRISTINE was previously Senior Independent Director and Chair of the "
             "Remuneration Committee at Standard Chartered Plc.")
    assert ground(page, quote) == (True, "normalized")


def test_fabricated_appointment_does_not_ground():
    page = load_pages("severn-trent")[10]  # Christine Hodgson's bio page
    assert "Standard Chartered" in page
    assert ground(page, "Non-executive Director of Standard Chartered") == (False, None)


def test_fuzzy_tolerates_a_small_extraction_slip():
    page = load_pages("tesco")[9]
    found, method = ground(page, "Non-executive director: Intercontinental Exchange Inc.")
    assert (found, method) == (True, "fuzzy")


def test_empty_quote_is_not_grounded():
    assert ground("anything", "") == (False, None)
    assert ground("anything", "   ") == (False, None)
    assert ground("", "quote") == (False, None)


def test_normalize_drops_zero_width_and_private_use_characters():
    assert normalize("Non\u200b-exec\u200dutive\ufeff Director") == "nonexecutive director"
    assert normalize("Chair\uf0b7Barratt Redrow plc") == "chair barratt redrow plc"
    page = "Senior\u200b independent director: The Rank\u200b Group plc"
    assert ground(page, "Senior independent director: The Rank Group plc") == (True, "normalized")


def test_normalize():
    assert normalize("Non-\nExecutive  Director’s\x07 role") == "nonexecutive director's role"
    assert normalize("multi-\nnational") == normalize("multinational")
    assert normalize("2008–2018") == "20082018"


def test_ground_near_tries_the_pages_either_side():
    pages = load_pages("tesco")
    quote = "Chair: Barratt Redrow plc and ICE Clear Europe"
    assert ground_near(pages, 10, quote) == (True, "exact", 10)
    assert ground_near(pages, 11, quote) == (True, "exact", 10)  # off by one
    assert ground_near(pages, 9, quote) == (True, "exact", 10)
    assert ground_near(pages, 12, quote) == (False, None, None)  # two pages away
    assert ground_near(pages, None, quote) == (True, "exact", 10)
    assert ground_near(pages, 99, quote) == (False, None, None)
