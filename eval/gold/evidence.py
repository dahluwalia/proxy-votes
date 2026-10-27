"""Companies House seats and evidence for the gold set (used by build_gold.py).

Design choice: one process for every director, no per-person exceptions.
- A person's CH records are the issuer's officer record for them plus any record with the
  same name and birth month and year (`ch_records` in evidence.yaml).
- Every director appointment on those records that is live on the AGM date, at a company
  CH shows as active, is a seat. A notice row at the same company gets the appointment as
  its board-seat evidence and its dates; a directorship no row covers becomes a seat of
  its own (role "director", from CH).
- A private CH company type means the shares can't be listed, so CH alone settles
  `listed: no`. Anything else needs a cited source in evidence.yaml.
- `check()` fails the build when a seat has no evidence, or when a seat that could count
  rests on an organisation with no evidence for its listing.
CH data is read from the raw recorded responses in tests/fixtures/ch/.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CH = HERE.parents[1] / "tests" / "fixtures" / "ch"
EVIDENCE = HERE / "evidence.yaml"
CH_WEB = "https://find-and-update.company-information.service.gov.uk"
API = "https://api.company-information.service.gov.uk"
PRIVATE_TYPES = ("ltd", "llp", "private-unlimited", "private-limited-guarant-nsc",
                 "private-limited-guarant-nsc-limited-exemption", "private-limited-shares-section-30-exemption",
                 "limited-partnership", "scottish-partnership", "charitable-incorporated-organisation")
ENDED = ("dissolved", "converted-closed", "closed", "liquidation", "removed")
_LEGAL = r"\b(plc|p l c|public limited company|limited|ltd|the|s a|inc|n v|group)\b"


def fold(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"['’]", "", s).replace("&", " and ")
    return " ".join(re.sub(_LEGAL, " ", re.sub(r"[^a-z0-9]+", " ", s)).split())


def _d(s):
    return date.fromisoformat(s) if s else None


def load():
    ev = yaml.safe_load(EVIDENCE.read_text()) if EVIDENCE.exists() else {}
    appts = json.loads((CH / "appointments.json").read_text())
    companies = json.loads((CH / "companies.json").read_text())
    return ev, appts, companies


def records(appts, officer_id):
    """(name, dob, appointments) of one CH officer record, every recorded page."""
    items, meta = [], {}
    for url, row in appts.items():
        if f"/officers/{officer_id}/appointments" in url and row["status"] == 200:
            items += row["body"].get("items", [])
            meta = row["body"]
    return meta.get("name"), meta.get("date_of_birth"), items


def ch_appointments(ev, appts, companies, person):
    """Every director appointment on the person's CH records, with company type and status."""
    out = []
    for oid in (ev.get("ch_records") or {}).get(person, []):
        name, dob, items = records(appts, oid)
        for a in items:
            role = a.get("officer_role", "")
            if "director" not in role or "secretary" in role:
                continue
            n = a["appointed_to"]["company_number"]
            prof = (companies.get(f"{API}/company/{n}") or {}).get("body") or {}
            out.append(dict(officer_id=oid, officer_name=name, dob=dob, number=n,
                            company=a["appointed_to"]["company_name"], role=role,
                            appointed_on=_d(a.get("appointed_on")), resigned_on=_d(a.get("resigned_on")),
                            company_type=prof.get("type"),
                            company_status=prof.get("company_status") or a["appointed_to"].get("company_status")))
    return out


def live_on(a, day) -> bool:
    return ((a["appointed_on"] is None or a["appointed_on"] <= day)
            and (a["resigned_on"] is None or a["resigned_on"] > day)
            and (a["company_status"] or "active") not in ENDED)


def ch_evidence(a) -> dict:
    ended = f", resigned {a['resigned_on']}" if a["resigned_on"] else ""
    return dict(source="ch", url=f"{CH_WEB}/officers/{a['officer_id']}/appointments",
                quote=f"{a['company']} ({a['number']}): {a['role']}, appointed {a['appointed_on']}{ended}")


def type_evidence(a) -> dict:
    return dict(source="ch", url=f"{CH_WEB}/company/{a['number']}",
                quote=f"Company type: {a['company_type']}; status: {a['company_status']}",
                supports="listed")


def match(org: str, number: str | None, a) -> bool:
    return (number is not None and number == a["number"]) or fold(org) == fold(a["company"])


def check(seats, fail):
    """Every seat has evidence; a seat that could count also has evidence for its org's listing;
    every web source carries a quote."""
    for s in seats:
        where = f'{s["seat_id"]} {s["director"]} @ {s["org"]}'
        if not s["evidence"]:
            fail(f"{where}: no evidence")
        if s["tense"] != "past" and s["board_seat"] != "no" and not s["listed_evidence"]:
            fail(f'{where}: no evidence for listed={s["listed"]}')
        if s.get("debt_listed", "no") != "no" and not any(
                e.get("supports") == "debt_listed" for e in s["listed_evidence"]):
            fail(f'{where}: no evidence for debt_listed={s["debt_listed"]}')
        for e in s["evidence"] + s["listed_evidence"]:
            if e.get("source", "web") == "web" and not e.get("quote"):
                fail(f'{where}: web source without a quote ({e.get("url")})')
