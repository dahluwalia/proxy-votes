"""M3.1 Accept: the parsers handle every row of every recorded CH fixture.

Each fixture file maps a CH API URL to {status, body, fetched_at}; every 200 body is
run through the parser for its endpoint. Skips until the fixtures are recorded.
"""

from __future__ import annotations

import json
import re

import pytest

from pennant.ch import parse
from pennant.ch.client import CH_FIXTURES
from pennant.dedup.names import is_title_or_honour

FILES = sorted(CH_FIXTURES.glob("*.json"))
pytestmark = pytest.mark.skipif(not FILES, reason="CH fixtures not recorded yet")


def rows():
    for path in FILES:
        for url, row in json.loads(path.read_text()).items():
            if row["status"] == 200:
                yield url, row["body"]


def test_every_fixture_row_parses():
    seen = {"company": 0, "officers": 0, "appointments": 0, "search": 0}
    for url, body in rows():
        if m := re.search(r"/company/([^/?]+)/officers", url):
            number = parse.pad(m.group(1))
            for item in body.get("items") or []:
                officer = parse.parse_officer_item(item, number, "X")
                assert officer and officer.appointments[0].company_number == number, item
                seen["officers"] += 1
        elif m := re.search(r"/company/([^/?]+)$", url):
            assert parse.parse_company(body)["company_number"] == parse.pad(m.group(1))
            seen["company"] += 1
        elif m := re.search(r"/officers/([^/]+)/appointments", url):
            officer = parse.parse_appointments(m.group(1), body)
            assert len(officer.appointments) == len(body.get("items") or [])
            seen["appointments"] += 1
        elif "/search/officers" in url:
            for item in body.get("items") or []:
                hit = parse.parse_search_item(item)
                assert hit and hit.surname, item
                *rest, last = hit.surname.split()
                assert not (rest and is_title_or_honour(last)), item  # "NEBHRAJANI OBE"
                seen["search"] += 1
        else:
            pytest.fail(f"no parser for fixture URL {url}")
    assert all(seen.values()), seen
