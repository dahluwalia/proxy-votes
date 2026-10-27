"""M3.2: record live Companies House responses as test fixtures. Needs CH_API_KEY.

Design choice: a fixture is just a CH cache row ({url: {status, body, fetched_at}}),
so offline tests run the same client code and get the same answers. The script runs
the real CH stage (client.run) and M4.2's expand() against an in-memory cache, then
writes every `ch` row to tests/fixtures/ch/<kind>.json. The cache is seeded from any
fixtures already recorded, so a re-run only fetches what is missing.

What gets recorded:
- the five issuers' profiles and officer lists
- for every director in office on their issuer's AGM date: appointments (company types
  for seats live on or after the earliest AGM), an officer search on their CH name,
  and the appointments of any search hit with the same DOB and exact name
- for the hard-case people: company profiles for every company in those appointment
  lists, resigned ones included

Usage: .venv/bin/python scripts/record_ch_fixtures.py
"""

from __future__ import annotations

import json
import sys
from datetime import date

from pennant.ch import client
from pennant.core import db
from pennant.core.config import load_policy, load_settings
from pennant.core.models import Meeting, RunContext, meeting_id
from pennant.dedup import anchor

ISSUERS = [  # company, number, AGM date (the CLAUDE.md meeting table)
    ("ITV plc", "04967001", date(2026, 5, 7)),
    ("Intertek Group plc", "04267576", date(2026, 5, 20)),
    ("Tesco PLC", "00445790", date(2026, 6, 18)),
    ("Marks and Spencer Group plc", "04256886", date(2026, 7, 7)),
    ("Severn Trent Plc", "02366619", date(2026, 7, 9)),
]
HARD_CASE_SURNAMES = {  # Kennedy, Nebhrajani, Ingram, Garnier, Hampton, Miles, Allen,
    "KENNEDY", "NEBHRAJANI", "INGRAM", "GARNIER", "HAMPTON", "MILES",  # Legg, Silver,
    "ALLEN", "LEGG", "SILVER", "GILLILAND", "MURPHY",  # Gilliland, Ken and Gerry Murphy
}


def kind_of(url: str) -> str:
    path = url.removeprefix(client.BASE_URL)
    if path.startswith("/search/"):
        return "search"
    if path.startswith("/officers/"):
        return "appointments"
    if "/officers?" in path:
        return "officers"
    return "companies"


SEARCH_FIELDS = ("title", "links", "date_of_birth", "description", "appointment_count")


def slim(kind: str, row: dict) -> dict:
    """Search hits keep only the fields the client reads, plus a description for humans.
    Dropping addresses and match snippets cuts search.json from ~7 MB to ~2 MB."""
    body = row.get("body")
    if kind != "search" or not body:
        return row
    items = [{k: i[k] for k in SEARCH_FIELDS if k in i} for i in body.get("items") or []]
    return {**row, "body": {**body, "items": items}}


def dump(conn, directory=client.CH_FIXTURES) -> dict[str, int]:
    """Write the newest `ch` cache row per URL to <directory>/<kind>.json."""
    files: dict[str, dict] = {}
    for url, payload, fetched_at in conn.execute(
        "SELECT key, payload, fetched_at FROM cache WHERE namespace = 'ch' ORDER BY id"
    ):
        kind = kind_of(url)
        files.setdefault(kind, {})[url] = slim(kind, {**json.loads(payload),
                                                     "fetched_at": fetched_at})
    directory.mkdir(parents=True, exist_ok=True)
    for kind, rows in files.items():
        text = json.dumps(rows, indent=1, sort_keys=True, ensure_ascii=False)
        (directory / f"{kind}.json").write_text(text + "\n")
    return {kind: len(rows) for kind, rows in sorted(files.items())}


def main() -> int:
    settings = load_settings()
    if not settings.ch_api_key:
        print("CH_API_KEY is missing from .env", file=sys.stderr)
        return 1
    conn = db.connect(":memory:")
    client.CH_EXPIRES = None  # recording fetches only what is missing, however old the rest is
    print(f"seeded {client.load_fixtures(conn)} rows from existing fixtures")
    run_id = db.create_run(conn, "record_ch_fixtures", author="record_ch_fixtures")
    ctx = RunContext(conn, run_id, settings, load_policy(), offline=False)
    db.put_outputs(conn, run_id, "meeting", [
        Meeting(meeting_id=meeting_id(n, agm), company_name=name, company_number=n,
                agm_date=agm, doc_sha="record")
        for name, n, agm in ISSUERS
    ])

    report = client.run(ctx)
    print(f"CH stage: ok={report.ok} {report.counts}")
    for err in report.errors:
        print(f"  error: {err}")
    if report.counts.get("issuers_ok", 0) == 0:
        print("no issuer officer list came back; is the key a Live key?", file=sys.stderr)
        return 1

    officers = db.get_outputs(conn, run_id, "officer")
    for i, officer in enumerate(officers, 1):
        found, reviews = anchor.expand(officer, ctx)
        hard = (officer.surname or "").upper() in HARD_CASE_SURNAMES
        if hard:
            for record in [officer] + [f for f, _ in found]:
                client.with_company_types(record, ctx, date.min)
        tags = (["hard case"] if hard else []) + [r.kind for r in reviews]
        print(f"[{i}/{len(officers)}] {officer.name}: {len(officer.appointments)} appointments, "
              f"{len(found)} linked {tags if tags else ''}")

    for kind, n in dump(conn).items():
        print(f"wrote {n} {kind} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
