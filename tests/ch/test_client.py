"""M3.1: the CH client against a fake transport. No network, no keys."""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from pennant.ch import client
from pennant.ch.parse import split_name
from pennant.core import db
from pennant.core.cache import get_cache, put_cache
from pennant.core.config import Settings
from pennant.core.models import Meeting, RunContext

TESCO = "00445790"


class Fake:
    """An httpx.MockTransport serving queued responses per path+query; records requests."""

    def __init__(self) -> None:
        self.routes: dict[str, list[httpx.Response | Exception]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, path: str, *responses: httpx.Response | Exception | int | dict) -> None:
        queue = self.routes.setdefault(path, [])
        for r in responses:
            if isinstance(r, int):
                r = httpx.Response(r)
            elif isinstance(r, dict):
                r = httpx.Response(200, json=r)
            queue.append(r)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode()
        queue = self.routes.get(path)
        if not queue:
            return httpx.Response(599, text=f"no fake route for {path}")
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def fake(monkeypatch) -> Fake:
    f = Fake()
    sleeps: list[float] = []
    monkeypatch.setattr(client, "TRANSPORT", httpx.MockTransport(f.handler))
    monkeypatch.setattr(client, "sleep", sleeps.append)
    f.sleeps = sleeps  # type: ignore[attr-defined]
    return f


@pytest.fixture
def live_ctx(ctx) -> RunContext:
    """The offline test ctx, switched online with a dummy key (the transport is fake)."""
    settings = Settings(ch_api_key="test-key", db_path=":memory:")
    return RunContext(ctx.conn, ctx.run_id, settings, ctx.policy, offline=False)


PROFILE = {"company_number": "445790", "company_name": "TESCO PLC", "type": "plc",
           "company_status": "active", "jurisdiction": "england-wales"}


def test_429_then_200_is_ok_and_cached(fake, live_ctx):
    fake.add(f"/company/{TESCO}", 429, PROFILE)
    res = client.company(TESCO, live_ctx)
    assert res.status == "ok"
    assert res.data["company_number"] == TESCO  # padded
    assert res.data["company_type"] == "plc"
    assert len(fake.requests) == 2
    assert client.company(TESCO, live_ctx).ok and len(fake.requests) == 2  # cache hit


def test_repeated_500s_are_unavailable_and_not_cached(fake, live_ctx):
    fake.add(f"/company/{TESCO}", 500)
    res = client.company(TESCO, live_ctx)
    assert res.status == "unavailable"
    assert "500" in res.error
    assert len(fake.requests) == client.MAX_TRIES
    assert get_cache(live_ctx.conn, "ch", client.BASE_URL + f"/company/{TESCO}") is None


def test_network_errors_are_retried(fake, live_ctx):
    fake.add(f"/company/{TESCO}", httpx.ConnectError("refused"), PROFILE)
    assert client.company(TESCO, live_ctx).ok


def test_offline_miss_is_unavailable_without_a_request(fake, ctx):
    res = client.company(TESCO, ctx)
    assert res.status == "unavailable"
    assert fake.requests == []


def test_404_is_not_found_and_cached(fake, live_ctx):
    fake.add("/company/09999999", 404)
    assert client.company("9999999", live_ctx).status == "not_found"
    assert client.company("9999999", live_ctx).status == "not_found"
    assert len(fake.requests) == 1


def test_rejected_key_is_unavailable_without_retry(fake, live_ctx):
    fake.add(f"/company/{TESCO}", 401)
    res = client.company(TESCO, live_ctx)
    assert res.status == "unavailable" and "rejected" in res.error
    assert len(fake.requests) == 1


def test_missing_key_is_unavailable(fake, ctx):
    online = RunContext(ctx.conn, ctx.run_id, Settings(), ctx.policy, offline=False)
    res = client.company(TESCO, online)
    assert res.status == "unavailable" and "CH_API_KEY" in res.error
    assert fake.requests == []


def test_basic_auth_key_as_username_blank_password(fake, live_ctx):
    fake.add(f"/company/{TESCO}", PROFILE)
    client.company(TESCO, live_ctx)
    expected = "Basic " + base64.b64encode(b"test-key:").decode()
    assert fake.requests[0].headers["Authorization"] == expected


def test_requests_are_throttled(fake, live_ctx):
    fake.add(f"/company/{TESCO}", PROFILE)
    fake.add("/company/04967001", PROFILE)
    client.company(TESCO, live_ctx)
    client.company("04967001", live_ctx)
    assert max(fake.sleeps) > client.MIN_INTERVAL - 0.1


def _officer(name: str, oid: str, role="director", appointed="2020-01-01", resigned=None,
             dob=(6, 1964)) -> dict:
    item = {"name": name, "officer_role": role, "appointed_on": appointed,
            "links": {"officer": {"appointments": f"/officers/{oid}/appointments"}},
            "date_of_birth": {"month": dob[0], "year": dob[1]}}
    if resigned:
        item["resigned_on"] = resigned
    return item


def test_officers_paginate_and_parse(fake, live_ctx):
    path = f"/company/{TESCO}/officers?items_per_page=100"
    page1 = [_officer(f"SMITH, John {i}", f"id{i}") for i in range(100)]
    page2 = [_officer("MURPHY, Kenneth", "ken"), _officer("MURPHY, Gerard", "gerry")]
    fake.add(f"{path}&start_index=0", {"total_results": 102, "items": page1})
    fake.add(f"{path}&start_index=100", {"total_results": 102, "items": page2})
    fake.add(f"/company/{TESCO}", PROFILE)
    res = client.officers(445790, live_ctx)
    assert res.ok and len(res.data) == 102
    ken = res.data[100]
    assert (ken.officer_id, ken.surname, ken.forenames) == ("ken", "MURPHY", "Kenneth")
    assert (ken.dob_month, ken.dob_year) == (6, 1964)
    assert ken.appointments[0].company_name == "TESCO PLC"
    assert ken.appointments[0].company_number == TESCO


def test_a_missing_page_makes_the_whole_list_unavailable(fake, live_ctx):
    path = f"/company/{TESCO}/officers?items_per_page=100"
    fake.add(f"{path}&start_index=0",
             {"total_results": 150, "items": [_officer(f"A, B{i}", f"i{i}") for i in range(100)]})
    fake.add(f"{path}&start_index=100", 503)
    assert client.officers(TESCO, live_ctx).status == "unavailable"


def test_appointments_fill_company_types_for_live_seats_only(fake, live_ctx):
    items = [
        {"appointed_to": {"company_number": "445790", "company_name": "TESCO PLC"},
         "officer_role": "director", "appointed_on": "2020-01-01",
         "name_elements": {"forename": "Kenneth", "surname": "MURPHY"}},
        {"appointed_to": {"company_number": "1234", "company_name": "OLD LTD"},
         "officer_role": "director", "appointed_on": "2001-01-01", "resigned_on": "2010-01-01"},
    ]
    fake.add("/officers/ken/appointments?items_per_page=50&start_index=0",
             {"name": "Kenneth MURPHY", "date_of_birth": {"month": 6, "year": 1964},
              "total_results": 2, "items": items})
    fake.add(f"/company/{TESCO}", PROFILE)
    res = client.appointments("ken", live_ctx, types_since=date(2026, 5, 7))
    assert res.ok
    tesco, old = res.data.appointments
    assert tesco.company_type == "plc" and old.company_type is None
    assert old.company_number == "00001234" and old.resigned_on == date(2010, 1, 1)
    assert (res.data.surname, res.data.forenames) == ("MURPHY", "Kenneth")


def test_search_parses_ids_names_and_dob(fake, live_ctx):
    hit = {"title": "Christopher Michael KENNEDY", "links": {"self": "/officers/ck1/appointments"},
           "date_of_birth": {"month": 3, "year": 1964}}
    fake.add("/search/officers?q=Christopher%20KENNEDY&items_per_page=100&start_index=0",
             {"total_results": 1, "items": [hit]})
    res = client.search_officers("Christopher KENNEDY", live_ctx)
    assert res.ok
    [hit] = res.data
    assert (hit.officer_id, hit.surname, hit.forenames) == ("ck1", "KENNEDY", "Christopher Michael")
    assert (hit.dob_month, hit.dob_year) == (3, 1964)


@pytest.mark.parametrize("name, expected", [
    ("KENNEDY, Christopher Michael", ("KENNEDY", "Christopher Michael")),
    ("BONHAM CARTER, Edward", ("BONHAM CARTER", "Edward")),
    ("Edward BONHAM CARTER", ("BONHAM CARTER", "Edward")),
    ("Hilde Merete AASHEIM", ("AASHEIM", "Hilde Merete")),
    ("John A SMITH", ("SMITH", "John A")),
    ("Jean-Michel Valette", ("Valette", "Jean-Michel")),
    # all upper case: no case signal, so read as lower case (last word is the surname),
    # the same as a mixed-case company name
    ("JOHN SMITH", ("SMITH", "JOHN")),
    ("CHRISTOPHER MICHAEL KENNEDY", ("KENNEDY", "CHRISTOPHER MICHAEL")),
    ("TESCO SECRETARIES LIMITED", ("LIMITED", "TESCO SECRETARIES")),
    ("SMITH", ("SMITH", None)),
    # honours and titles are not part of the surname (CH search titles)
    ("Sharmila NEBHRAJANI  OBE", ("NEBHRAJANI", "Sharmila")),
    ("Carolyn Julia MCCALL DBE", ("MCCALL", "Carolyn Julia")),
    ("Christine Mary HODGSON CBE BSC (HONS) FCA", ("HODGSON", "Christine Mary")),
    ("NEBHRAJANI OBE, Sharmila", ("NEBHRAJANI", "Sharmila")),
    ("MCCALL, Carolyn Julia, Dame", ("MCCALL", "Carolyn Julia")),
    ("Jack MA", ("MA", "Jack")),  # kept: the surname is all there is
    ("MA, Jack", ("MA", "Jack")),
])
def test_split_name(name, expected):
    assert split_name(name) == expected


def test_stage_writes_directors_in_office_and_fails_closed(fake, live_ctx):
    conn, run = live_ctx.conn, live_ctx.run_id
    db.put_outputs(conn, run, "meeting", [
        Meeting(meeting_id=f"mtg:{TESCO}:2026-06-18", company_name="Tesco PLC",
                company_number=TESCO, agm_date=date(2026, 6, 18), doc_sha="x"),
        Meeting(meeting_id="mtg:04967001:2026-05-07", company_name="ITV plc",
                company_number="04967001", agm_date=date(2026, 5, 7), doc_sha="y"),
    ])
    fake.add(f"/company/{TESCO}/officers?items_per_page=100&start_index=0", {
        "total_results": 5, "items": [
            _officer("MURPHY, Kenneth", "ken"),
            _officer("GONE, Long", "gone", resigned="2024-01-01"),
            _officer("RECENT, Left", "recent", resigned="2026-05-06"),
            _officer("LATER, Joined", "later", appointed="2026-09-01"),
            _officer("SECRETARY, Company", "sec", role="secretary"),
        ]})
    fake.add(f"/company/{TESCO}", PROFILE)
    fake.add("/officers/ken/appointments?items_per_page=50&start_index=0",
             {"name": "Kenneth MURPHY", "total_results": 0, "items": []})
    fake.add("/company/04967001/officers?items_per_page=100&start_index=0", 503)

    report = client.run(live_ctx)
    assert not report.ok
    assert report.counts["issuers"] == 2 and report.counts["issuers_ok"] == 1
    # in office -> full record; resigned within the year -> issuer seat only, no fetch
    written = {o.officer_id: o for o in db.get_outputs(conn, run, "officer")}
    assert sorted(written) == ["ken", "recent"]
    assert written["recent"].appointments[0].resigned_on == date(2026, 5, 6)
    assert report.counts["recently_resigned"] == 1
    assert not any("/officers/recent/" in str(r.url) for r in fake.requests)
    [review] = db.get_outputs(conn, run, "review")
    assert review.kind == "ch_unavailable" and review.subject == "mtg:04967001:2026-05-07"


SEARCH = "/search/officers?q=Jane%20SMITH&items_per_page=100"


def _hits(names_ids, dob=(1, 1970)):
    return [{"title": n, "links": {"self": f"/officers/{i}/appointments"},
             "date_of_birth": {"month": dob[0], "year": dob[1]}} for n, i in names_ids]


def _page(prefix, n, name="Jane SMITH"):
    return _hits([(name, f"{prefix}{i}") for i in range(n)])


def test_search_reads_on_while_namesakes_continue(fake, live_ctx):
    """CH ranks thousands of fuzzy matches; a fixed page count can cut a block of
    same-name officers in two. Reading stops at the first page with none of them."""
    fake.add(f"{SEARCH}&start_index=0", {"total_results": 1000, "items": _page("a", 100)})
    fake.add(f"{SEARCH}&start_index=100", {"total_results": 1000, "items": _page("b", 100)})
    fake.add(f"{SEARCH}&start_index=200",
             {"total_results": 1000, "items": _page("c", 100, "Peter SMITH")})
    res = client.search_officers("Jane SMITH", live_ctx)
    assert res.ok and res.complete and len(res.data) == 300 and res.unread == 700
    assert len(fake.requests) == 3


def test_search_stopped_by_the_cap_amid_namesakes_is_incomplete(fake, live_ctx, monkeypatch):
    monkeypatch.setattr(client, "SEARCH_MAX_PAGES", 2)
    for start in (0, 100):
        fake.add(f"{SEARCH}&start_index={start}",
                 {"total_results": 1000, "items": _page(f"p{start}-", 100)})
    res = client.search_officers("Jane SMITH", live_ctx)
    assert res.ok and not res.complete and res.unread == 800 and len(fake.requests) == 2


def _search_rows(conn):
    return conn.execute("SELECT payload FROM cache WHERE key = ?",
                        (client.BASE_URL + SEARCH,)).fetchall()


@pytest.fixture
def stale(monkeypatch):
    """Every stored CH answer is past its expiry, as on the next night's run."""
    monkeypatch.setattr(client, "_end_of_fetch_day", lambda f: f - timedelta(seconds=1))


def test_reordered_search_with_a_drifting_tail_keeps_the_stored_entry(fake, live_ctx, stale):
    """Same namesakes in another order, with different non-namesakes around them: no new
    row, and the run sees the stored answer, so a replay matches exactly."""
    jane = _hits([("Jane SMITH", "j1"), ("Jane Ann SMITH", "j2")])
    first = jane + _hits([("Peter SMITH", "x1")])
    second = jane[::-1] + _hits([("Paul SMITH", "x2")])
    fake.add(f"{SEARCH}&start_index=0", {"total_results": 3, "items": first},
             {"total_results": 3, "items": second})
    one = client.search_officers("Jane SMITH", live_ctx)
    two = client.search_officers("Jane SMITH", live_ctx)
    assert len(fake.requests) == 2 and len(_search_rows(live_ctx.conn)) == 1
    assert [h.officer_id for h in two.data] == [h.officer_id for h in one.data]


def test_search_with_a_new_namesake_is_a_new_entry(fake, live_ctx, stale):
    jane = _hits([("Jane SMITH", "j1")])
    fake.add(f"{SEARCH}&start_index=0", {"total_results": 1, "items": jane},
             {"total_results": 2, "items": jane + _hits([("Jane SMITH", "j9")])})
    client.search_officers("Jane SMITH", live_ctx)
    res = client.search_officers("Jane SMITH", live_ctx)
    assert "j9" in {h.officer_id for h in res.data} and len(_search_rows(live_ctx.conn)) == 2


def test_recorded_search_pages_load_as_one_entry_per_name(ctx, tmp_path):
    url = client.BASE_URL + SEARCH
    rows = {f"{url}&start_index={i * 100}": {
        "status": 200, "fetched_at": "2026-10-26T01:00:00+00:00",
        "body": {"total_results": 150, "items": _page(f"p{i}-", 100 if i == 0 else 50)}}
        for i in (1, 0)}
    (tmp_path / "search.json").write_text(json.dumps(rows))
    assert client.load_fixtures(ctx.conn, tmp_path) == 1
    res = client.search_officers("Jane SMITH", ctx)  # offline: served from the fixture
    assert res.ok and [h.officer_id for h in res.data][:1] == ["p0-0"] and len(res.data) == 150


def test_stage_keeps_the_issuer_seat_when_the_appointments_list_lacks_it(fake, live_ctx):
    """The officer-list record is merged into the appointments record, not replaced by it,
    so an empty appointments list (and one with no name or DOB) can't unanchor anyone."""
    conn, run = live_ctx.conn, live_ctx.run_id
    db.put_outputs(conn, run, "meeting", [
        Meeting(meeting_id=f"mtg:{TESCO}:2026-06-18", company_name="Tesco PLC",
                company_number=TESCO, agm_date=date(2026, 6, 18), doc_sha="x")])
    fake.add(f"/company/{TESCO}/officers?items_per_page=100&start_index=0",
             {"total_results": 1, "items": [_officer("MURPHY, Kenneth", "ken")]})
    fake.add(f"/company/{TESCO}", PROFILE)
    fake.add("/officers/ken/appointments?items_per_page=50&start_index=0",
             {"total_results": 0, "items": []})
    assert client.run(live_ctx).ok
    [ken] = db.get_outputs(conn, run, "officer")
    assert (ken.surname, ken.forenames, ken.dob_year) == ("MURPHY", "Kenneth", 1964)
    [seat] = ken.appointments
    assert (seat.company_number, seat.role, seat.company_type) == (TESCO, "director", "plc")


def test_a_day_old_answer_is_fetched_again_online_and_kept_offline(fake, live_ctx, ctx):
    """A CH answer is good until 23:59 local time on the day it was fetched, so the nightly
    run sees new appointments; offline (analyst re-runs, tests) serves what is stored."""
    url = client.BASE_URL + "/company/00445790"
    put_cache(ctx.conn, "ch", url, {"status": 200, "body": PROFILE},
              fetched_at=datetime.now(UTC) - timedelta(days=1))  # yesterday
    assert client.company("00445790", ctx).ok and fake.requests == []  # offline: stored
    fake.add("/company/00445790", {**PROFILE, "company_status": "dissolved"})
    res = client.company("00445790", live_ctx)
    assert res.data["company_status"] == "dissolved" and len(fake.requests) == 1
    client.company("00445790", live_ctx)  # fetched a moment ago: no new request
    assert len(fake.requests) == 1


def test_answers_go_stale_at_2359_local_on_the_day_they_were_fetched():
    fetched = datetime(2026, 10, 27, 6, 5).astimezone()  # 06:05 local
    end = client._end_of_fetch_day(fetched)
    assert (end.date(), end.hour, end.minute) == (fetched.date(), 23, 59)
