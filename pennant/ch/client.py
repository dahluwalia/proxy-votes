"""Companies House client: the only code that talks to CH.

Design choice: every call returns a CHResult whose status is `ok`, `not_found` or
`unavailable`. It never raises, so an outage becomes "unknown", not "no seats".
Each GET goes through core.cache, keyed on its URL (one row per page), and stores
{"status", "body"}. 200s and 404s are cached; failures never are. A cached answer is
good until 23:59 local time on the day it was last fetched (CH_EXPIRES), so the nightly
run, and any later live run, fetches it again and sees new appointments and resignations;
an unchanged answer only moves the row's last-fetched date. Offline runs never refetch.
An officer search is one cached answer per name, not per page. CH returns thousands of
fuzzy matches and ranks ties differently on every request, so the client reads pages
until it is past the officers who share the searched surname and first forename (the
only hits that can be the same person), and a refetch counts as unchanged when those
namesakes are the same, whatever their order or the hits around them. Requests are
throttled to 2 a second, and 429/5xx responses are retried with backoff.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx

from pennant import logs
from pennant.ch.parse import (
    active_on,
    namesakes,
    pad,
    parse_appointments,
    parse_company,
    parse_officer_item,
    parse_search_item,
)
from pennant.core import db
from pennant.core.cache import cached, get_cache, put_cache
from pennant.core.config import FIXTURES_DIR
from pennant.core.models import CHOfficer, ReviewItem, RunContext, StageReport

BASE_URL = "https://api.company-information.service.gov.uk"
WEB_URL = "https://find-and-update.company-information.service.gov.uk"  # for citations
CH_FIXTURES = FIXTURES_DIR / "ch"
MIN_INTERVAL = 0.5  # seconds between requests: at most 2 a second
MAX_TRIES = 5
BACKOFF = 1.0  # first retry delay in seconds; doubles each time
MAX_PAGES = 20
SEARCH_PAGE = 100  # hits per search page
SEARCH_MAX_PAGES = 10  # read on while namesakes continue, up to 1,000 hits
CH_EXPIRES: clock | None = clock(23, 59)  # local time: answers are good until then that day
RECENTLY_RESIGNED = timedelta(days=365)  # still written by run(), to explain unanchored names

TRANSPORT: httpx.BaseTransport | None = None  # tests swap in httpx.MockTransport
sleep = time.sleep  # tests swap in a no-op
log = logs.get("ch")
_last_request = 0.0

Status = Literal["ok", "not_found", "unavailable"]


@dataclass(frozen=True)
class CHResult:
    status: Status
    data: Any = None
    url: str = ""
    error: str | None = None
    fetched_at: datetime | None = None
    unread: int = 0  # list items CH reported but this call did not read (search pages)
    complete: bool = True  # False: a search stopped at SEARCH_MAX_PAGES amid namesakes

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# --- transport ---------------------------------------------------------------


def _http_get(url: str, api_key: str) -> tuple[Status, Any]:
    """One throttled GET with retries -> (ok, body), (not_found, None) or (unavailable, why)."""
    global _last_request
    why, delay = "", 0.0
    for attempt in range(MAX_TRIES):
        if attempt:
            logs.count("ch.retries")
            log.info("CH retry %d for %s after %s", attempt, url, why)
        sleep(max(delay, MIN_INTERVAL - (time.monotonic() - _last_request), 0.0))
        _last_request = time.monotonic()
        logs.count("ch.requests")
        try:
            with httpx.Client(auth=(api_key, ""), transport=TRANSPORT, timeout=30.0) as http:
                r = http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:  # timeouts, refused connections: retry
            why, delay = f"{type(e).__name__}: {e}", max(BACKOFF, delay * 2)
            continue
        if r.status_code == 200:
            try:
                return "ok", r.json()
            except ValueError:
                return "unavailable", "HTTP 200 with a body that isn't JSON"
        if r.status_code == 404:
            return "not_found", None
        if r.status_code in (401, 403):
            return "unavailable", f"HTTP {r.status_code}: CH_API_KEY rejected"
        why = f"HTTP {r.status_code}"
        if r.status_code != 429 and r.status_code < 500:
            return "unavailable", why
        retry_after = r.headers.get("Retry-After", "")
        delay = float(retry_after) if retry_after.isdigit() else max(BACKOFF, delay * 2)
    return "unavailable", f"{why} after {MAX_TRIES} tries"


def _end_of_fetch_day(fetched: datetime) -> datetime:
    """CH_EXPIRES (local time) on the day of the fetch: when a cached answer goes stale."""
    assert CH_EXPIRES is not None
    local = fetched.astimezone()
    return local.replace(hour=CH_EXPIRES.hour, minute=CH_EXPIRES.minute, second=59,
                         microsecond=0)


def get(path: str, ctx: RunContext) -> CHResult:
    """GET one CH API path through the cache. Never raises."""
    url = BASE_URL + path
    return _through_cache(url, ctx, lambda key: _http_get(url, key))


def _through_cache(url: str, ctx: RunContext, http, same=None) -> CHResult:
    """`http(api_key) -> (status, body)` behind the cache, keyed on `url`. Never raises."""
    failure: list[str] = []

    def fetch() -> dict[str, Any] | None:
        if not ctx.settings.ch_api_key:
            failure.append("CH_API_KEY missing from .env")
            return None
        status, body = http(ctx.settings.ch_api_key)
        if status == "unavailable":
            failure.append(body)
            return None
        return {"status": 200 if status == "ok" else 404, "body": body}

    try:
        hit = cached(ctx.conn, "ch", url, fetch, offline=ctx.offline,
                     fresh_until=_end_of_fetch_day if CH_EXPIRES else None, same=same)
        stamp = get_cache(ctx.conn, "ch", url)
    except Exception as e:  # fail closed: a broken cache or bug is "unavailable"
        log.exception("CH call failed: %s", url)
        return CHResult("unavailable", url=url, error=f"{type(e).__name__}: {e}")
    if hit is None:
        why = failure[0] if failure else "offline and not in the cache"
        logs.count("ch.unavailable")
        log.warning("CH unavailable: %s: %s", url.removeprefix(BASE_URL), why)
        return CHResult("unavailable", url=url, error=why)
    fetched_at = stamp[1] if stamp else None
    if hit.get("status") == 404:
        return CHResult("not_found", url=url, fetched_at=fetched_at)
    return CHResult("ok", hit.get("body") or {}, url=url, fetched_at=fetched_at)


def _pages(path: str, ctx: RunContext, per_page: int, max_pages: int) -> CHResult:
    """Every page of a CH list, merged into one body. Unavailable if any page is."""
    sep = "&" if "?" in path else "?"
    first: CHResult | None = None
    items: list[dict[str, Any]] = []
    for _ in range(max_pages):
        res = get(f"{path}{sep}items_per_page={per_page}&start_index={len(items)}", ctx)
        if res.status == "unavailable" or (first is None and not res.ok):
            return res
        if not res.ok:  # a 404 past the first page means the list has ended
            break
        first = first or res
        page = res.data.get("items") or []
        items += page
        if not page or len(items) >= int(res.data.get("total_results") or 0):
            break
    assert first is not None
    return replace(first, data={**first.data, "items": items})


# --- endpoints ---------------------------------------------------------------


def company(n: Any, ctx: RunContext) -> CHResult:
    """GET /company/{n} -> data: company_number, company_name, company_type, company_status."""
    res = get(f"/company/{pad(n)}", ctx)
    return replace(res, data=parse_company(res.data)) if res.ok else res


def officers(n: Any, ctx: RunContext) -> CHResult:
    """GET /company/{n}/officers, every page -> data: list[CHOfficer], each holding its one
    appointment at this company (resigned officers included)."""
    number = pad(n)
    res = _pages(f"/company/{number}/officers", ctx, 100, MAX_PAGES)
    if not res.ok:
        return res
    profile = company(number, ctx)
    name = profile.data["company_name"] if profile.ok else number
    kind = profile.data["company_type"] if profile.ok else None
    parsed = [parse_officer_item(i, number, name, kind) for i in res.data["items"]]
    return replace(res, data=[o for o in parsed if o])


def appointments(officer_id: str, ctx: RunContext, types_since: date | None = None) -> CHResult:
    """GET /officers/{id}/appointments, every page -> data: CHOfficer with all appointments.

    With `types_since`, company_type is filled from the company profile for each appointment
    not resigned before that date. A failed profile leaves it None (unknown)."""
    res = _pages(f"/officers/{quote(officer_id, safe='')}/appointments", ctx, 50, MAX_PAGES)
    if not res.ok:
        return res
    officer = parse_appointments(officer_id, res.data)
    if types_since is not None:
        officer = with_company_types(officer, ctx, types_since)
    return replace(res, data=officer)


def with_company_types(officer: CHOfficer, ctx: RunContext, since: date) -> CHOfficer:
    appts = []
    for a in officer.appointments:
        if a.company_type is None and (a.resigned_on is None or a.resigned_on >= since):
            profile = company(a.company_number, ctx)
            if profile.ok:
                a = a.model_copy(update={"company_type": profile.data["company_type"]})
        appts.append(a)
    return officer.model_copy(update={"appointments": appts})


def _search_pages(url: str, name: str, api_key: str) -> tuple[Status, Any]:
    """Every page up to the first with no namesakes (or the last page, or the cap), merged.
    `complete` is False when the cap cut off a page that still had namesakes."""
    items: list[dict[str, Any]] = []
    total, complete = 0, True
    for n in range(SEARCH_MAX_PAGES):
        status, body = _http_get(f"{url}&start_index={len(items)}", api_key)
        if status == "unavailable" or (status == "not_found" and n == 0):
            return status, body
        page = (body or {}).get("items") or [] if status == "ok" else []
        items += page
        total = int((body or {}).get("total_results") or total) if status == "ok" else total
        if not page or len(items) >= total or not namesakes({"items": page}, name):
            break
        complete = n + 1 < SEARCH_MAX_PAGES
    return "ok", {"total_results": total, "items": items, "complete": complete}


def search_officers(name: str, ctx: RunContext) -> CHResult:
    """GET /search/officers?q=<name> -> data: list[CHOfficer] (officer id, name and DOB
    month/year; no appointments), read until past the namesakes. One cache entry per name;
    a refetch with the same namesakes keeps the stored entry. `unread` counts the hits
    CH reported beyond the pages read."""
    url = f"{BASE_URL}/search/officers?q={quote(name.strip())}&items_per_page={SEARCH_PAGE}"

    def same(old: dict[str, Any], new: dict[str, Any]) -> bool:
        return old.get("status") == new.get("status") and \
            namesakes(old.get("body"), name) == namesakes(new.get("body"), name)

    res = _through_cache(url, ctx, lambda key: _search_pages(url, name, key), same)
    if not res.ok:
        return res
    items = res.data.get("items") or []
    unread = max(int(res.data.get("total_results") or 0) - len(items), 0)
    parsed = [parse_search_item(i) for i in items]
    return replace(res, data=[o for o in parsed if o], unread=unread,
                   complete=res.data.get("complete", True))


# --- stage -------------------------------------------------------------------


_IDENTITY = ("name", "surname", "forenames", "dob_month", "dob_year")


def _merge(a: CHOfficer, b: CHOfficer) -> CHOfficer:
    """`a` plus `b`'s appointments it lacks; `b` also fills any name or DOB `a` lacks."""
    seen = {(x.company_number, x.role, x.appointed_on) for x in a.appointments}
    extra = [x for x in b.appointments if (x.company_number, x.role, x.appointed_on) not in seen]
    fill = {k: getattr(b, k) for k in _IDENTITY if getattr(a, k) in (None, "")}
    return a.model_copy(update={**fill, "appointments": a.appointments + extra})


def run(ctx: RunContext) -> StageReport:
    """CH stage. For each meeting, fetch the issuer's officer list (gate G2 needs every one
    `ok`), then the full appointments of each director in office on the AGM date.

    Writes those directors as `officer` outputs, merged by officer_id across issuers, and
    each failure as a `ch_unavailable` review item. Directors who resigned in the year
    before the AGM are written too, with just their issuer appointment and no extra
    fetch, so an unanchored notice name can be explained ("resigned 2026-05-06").
    `ok` is False when any issuer's officer list is not ok."""
    meetings = db.get_outputs(ctx.conn, ctx.run_id, "meeting")
    since = min((m.agm_date for m in meetings), default=None)
    found: dict[str, CHOfficer] = {}
    recent: dict[str, CHOfficer] = {}
    reviews: list[ReviewItem] = []
    errors: list[str] = []
    issuers_ok = 0
    for m in meetings:
        res = officers(m.company_number, ctx)
        if not res.ok:
            errors.append(f"officers {m.company_number}: {res.status} ({res.error})")
            reviews.append(ReviewItem(
                kind="ch_unavailable", subject=m.meeting_id,
                detail=f"CH officer list for {m.company_name} is {res.status}: {res.error}",
                suggested_command="pennant run"))
            continue
        issuers_ok += 1
        for o in res.data:
            appt = o.appointments[0]
            if "director" not in appt.role:
                continue
            if not active_on(appt, m.agm_date):
                if appt.resigned_on and appt.resigned_on >= m.agm_date - RECENTLY_RESIGNED:
                    recent[o.officer_id] = _merge(recent[o.officer_id], o) \
                        if o.officer_id in recent else o
                continue
            full = appointments(o.officer_id, ctx, since)
            if not full.ok:
                errors.append(f"appointments {o.officer_id}: {full.status} ({full.error})")
                reviews.append(ReviewItem(
                    kind="ch_unavailable", subject=o.officer_id,
                    detail=f"CH appointments for {o.name} are {full.status}: {full.error}; "
                    "their other seats are unknown",
                    suggested_command="pennant run"))
            # Merged, not replaced: an appointments list that lacks the issuer seat must
            # not leave the director unanchored.
            record = _merge(full.data, o) if full.ok else o
            found[o.officer_id] = _merge(found[o.officer_id], record) if o.officer_id in found \
                else record
    unavailable = sum(r.subject in found for r in reviews)
    recent = {k: v for k, v in recent.items() if k not in found}
    db.put_outputs(ctx.conn, ctx.run_id, "officer", [*found.values(), *recent.values()])
    db.put_outputs(ctx.conn, ctx.run_id, "review", reviews)
    counts = {"issuers": len(meetings), "issuers_ok": issuers_ok, "officers": len(found),
              "recently_resigned": len(recent), "appointments_unavailable": unavailable}
    return StageReport(stage="ch", ok=issuers_ok == len(meetings), counts=counts, errors=errors)


# --- fixtures ----------------------------------------------------------------


def load_fixtures(conn: Any, directory: Path | str | None = None) -> int:
    """Seed the `ch` cache from recorded responses (tests/fixtures/ch/*.json, each
    {url: {status, body, fetched_at}}). Returns the number of rows loaded. Search pages
    recorded one per URL (`...&start_index=N`) are joined into the one entry per name
    that search_officers reads."""
    rows: dict[str, dict[str, Any]] = {}
    pages: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for path in sorted(Path(directory or CH_FIXTURES).glob("*.json")):
        for url, row in json.loads(path.read_text()).items():
            base, _, start = url.partition("&start_index=")
            if "/search/officers?" in url and start.isdigit():
                pages.setdefault(base, []).append((int(start), row))
            else:
                rows[url] = row
    for base, parts in pages.items():
        parts.sort(key=lambda p: p[0])
        first = parts[0][1]
        items = [i for _, r in parts for i in (r.get("body") or {}).get("items") or []]
        rows[base] = {"status": first["status"], "body": {**(first.get("body") or {}),
                      "items": items}, "fetched_at": max(r["fetched_at"] for _, r in parts)}
    for url, row in rows.items():
        payload = {"status": row["status"], "body": row["body"]}
        put_cache(conn, "ch", url, payload, datetime.fromisoformat(row["fetched_at"]))
    return len(rows)
