"""What we ask LlamaExtract for, and how its answer becomes a NoticeExtract.

Design choice: the JSON schema sent to LlamaExtract and the pydantic models we
parse into are kept side by side, and parsing is a pure function over the raw
job JSON. The raw response is what gets cached and committed as a fixture, so a
parser fix never costs credits. Each citation (page, matching text) is mapped
onto the field it supports as notice Evidence. Values outside the schema fail
closed: an odd tense becomes `unclear`, an odd coverage becomes `none`, and both
add a warning that M1.2 turns into a review item.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from pennant.core.models import Coverage, Evidence, notice_ref, parse_notice_ref

# Bump whenever DATA_SCHEMA changes: it is part of the cache key, and the committed
# fixtures record schema_sha() so a silent edit is caught by the tests.
SCHEMA_VERSION = "v1"

Tense = Literal["current", "past", "future", "unclear"]
Section = Literal["current_appointments", "narrative"]
TENSES = ("current", "past", "future")
SECTIONS = ("current_appointments", "narrative")
COVERAGES = ("structured", "prose", "none")


def _field(type_: str | None, description: str, **extra: Any) -> dict[str, Any]:
    typed = {"type": type_} if type_ else {"anyOf": [{"type": "string"}, {"type": "null"}]}
    return {**typed, **extra, "description": description}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties)}


APPOINTMENT_SCHEMA = _object({
    "organisation": _field(
        "string", "One organisation, exactly as named, e.g. 'Barratt Redrow plc'."),
    "role": _field("string", "The role held there, as written, e.g. 'Chair', "
                   "'Non-executive director', 'Chief executive officer', 'Trustee'."),
    "tense": _field("string", "past for previously/formerly/until/retired/was; future for "
                    "will/from the conclusion of the AGM; current otherwise.", enum=list(TENSES)),
    "section": _field("string", "current_appointments if listed under a heading like 'Key "
                      "current appointments'; otherwise narrative.", enum=list(SECTIONS)),
    "quote": _field("string", "The exact words from the document that state this appointment, "
                    "copied verbatim (the whole line for a compound list)."),
})

DIRECTOR_SCHEMA = _object({
    "resolution_no": _field(
        "integer", "Number of the resolution electing or re-electing this director."),
    "name": _field("string", "Name as written in the resolution, including titles and honours."),
    "role_at_company": _field(None, "Role at the company holding this AGM, as written in this "
                              "document; null if not stated."),
    "coverage": _field("string", "structured if the biography has a current-appointments list "
                       "(even 'None'); prose if narrative only; none if no biography in this "
                       "document.", enum=list(COVERAGES)),
    "appointments": _field("array", "Roles at other organisations, current, past or future. "
                           "One entry per organisation and role.", items=APPOINTMENT_SCHEMA),
})

DATA_SCHEMA = _object({
    "company_name": _field("string", "Name of the company holding the AGM, e.g. 'Tesco PLC'."),
    "company_number": _field(
        "string", "Companies House registered number as printed, e.g. '00445790'."),
    "agm_date": _field("string", "Date of the Annual General Meeting as YYYY-MM-DD."),
    "directors": _field("array", "Only people named in director election or re-election "
                        "resolutions.", items=DIRECTOR_SCHEMA),
})


def schema_sha() -> str:
    return hashlib.sha256(json.dumps(DATA_SCHEMA, sort_keys=True).encode()).hexdigest()[:16]


# --- parsed result -----------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


FieldEvidence = dict[str, list[Evidence]]  # field name -> its citations


def _pages(evidence: FieldEvidence, first: str) -> list[int]:
    """Cited pages, the `first` field's citations before the rest, without repeats."""
    ordered = evidence.get(first, []) + [e for k, v in evidence.items() if k != first for e in v]
    pages: list[int] = []
    for ev in ordered:
        page = parse_notice_ref(ev.ref)[1]
        if page is not None and page not in pages:
            pages.append(page)
    return pages


class ExtractedAppointment(_Model):
    organisation: str
    role: str
    tense: Tense
    section: Section
    quote: str
    evidence: FieldEvidence = {}

    def cited_pages(self) -> list[int]:
        return _pages(self.evidence, "quote")


class ExtractedDirector(_Model):
    resolution_no: int | None
    name: str
    role_at_company: str | None = None
    coverage: Coverage
    appointments: list[ExtractedAppointment] = []
    evidence: FieldEvidence = {}

    def cited_pages(self, first: str = "name") -> list[int]:
        return _pages(self.evidence, first)


class NoticeExtract(_Model):
    doc: str  # PDF filename
    doc_sha: str
    job_id: str | None = None
    company_name: str | None = None
    company_number: str | None = None  # as printed; M1.2 pads and cross-checks it
    agm_date: str | None = None  # as returned; M1.2 parses and cross-checks it
    directors: list[ExtractedDirector] = []
    evidence: FieldEvidence = {}
    warnings: list[str] = []


# --- parsing -----------------------------------------------------------------


def _citations(meta: Any, doc: str, fetched_at: str | None) -> list[Evidence]:
    """Evidence for one leaf field's metadata: {"citation": [{"page", "matching_text"}]}."""
    if not isinstance(meta, dict):
        return []
    out = []
    for c in meta.get("citation") or meta.get("citations") or []:
        if not isinstance(c, dict):
            continue
        page = c.get("page", c.get("page_number"))
        if isinstance(page, str) and page.strip().isdigit():
            page = int(page)
        if not isinstance(page, int) or isinstance(page, bool):
            continue
        text = c.get("matching_text") or c.get("text")
        out.append(
            Evidence(source="notice", ref=notice_ref(doc, page), quote=text, fetched_at=fetched_at)
        )
    return out


def _field_evidence(meta: Any, fields: tuple[str, ...], doc: str, at: str | None) -> FieldEvidence:
    meta = meta if isinstance(meta, dict) else {}
    found = {f: _citations(meta.get(f), doc, at) for f in fields}
    return {f: evs for f, evs in found.items() if evs}


def _item(seq: Any, i: int) -> Any:
    return seq[i] if isinstance(seq, list) and i < len(seq) else None


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _choice(value: Any, allowed: tuple[str, ...], fallback: str, what: str, warn: list[str]):
    v = (_str(value) or "").lower().replace(" ", "_")
    if v in allowed:
        return v
    warn.append(f"{what}: {value!r} is not one of {', '.join(allowed)}; using {fallback}")
    return fallback


def parse_job(raw: dict[str, Any], doc: str, doc_sha: str) -> NoticeExtract:
    """Turn a raw LlamaExtract job (ExtractV2Job as JSON) into a NoticeExtract."""
    result = raw.get("extract_result")
    if isinstance(result, list):  # per_page output; per_doc gives one object
        result = result[0] if len(result) == 1 else None
    if not isinstance(result, dict):
        raise ValueError(f"{doc}: extract_result is not a single object")
    meta = ((raw.get("extract_metadata") or {}).get("field_metadata") or {}).get(
        "document_metadata"
    ) or {}
    at = raw.get("updated_at") or raw.get("created_at")
    warn: list[str] = []
    directors = []
    for i, d in enumerate(result.get("directors") or []):
        d_meta = _item(meta.get("directors"), i)
        name = _str(d.get("name")) or ""
        label = f"director {i} ({name or 'no name'})"
        resolution_no = _int(d.get("resolution_no"))
        if resolution_no is None:
            warn.append(f"{label}: resolution_no {d.get('resolution_no')!r} is not a number")
        appointments = []
        for j, a in enumerate(d.get("appointments") or []):
            a_meta = _item((d_meta or {}).get("appointments"), j)
            what = f"{label} appointment {j}"
            appointments.append(
                ExtractedAppointment(
                    organisation=_str(a.get("organisation")) or "",
                    role=_str(a.get("role")) or "",
                    tense=_choice(a.get("tense"), TENSES, "unclear", f"{what} tense", warn),
                    section=_choice(
                        a.get("section"), SECTIONS, "narrative", f"{what} section", warn
                    ),
                    quote=_str(a.get("quote")) or "",
                    evidence=_field_evidence(
                        a_meta, ("quote", "organisation", "role", "tense", "section"), doc, at
                    ),
                )
            )
        directors.append(
            ExtractedDirector(
                resolution_no=resolution_no,
                name=name,
                role_at_company=_str(d.get("role_at_company")),
                coverage=_choice(d.get("coverage"), COVERAGES, "none", f"{label} coverage", warn),
                appointments=appointments,
                evidence=_field_evidence(
                    d_meta, ("name", "resolution_no", "role_at_company", "coverage"), doc, at
                ),
            )
        )
    return NoticeExtract(
        doc=doc,
        doc_sha=doc_sha,
        job_id=_str(raw.get("id")),
        company_name=_str(result.get("company_name")),
        company_number=_str(result.get("company_number")),
        agm_date=_str(result.get("agm_date")),
        directors=directors,
        evidence=_field_evidence(meta, ("company_name", "company_number", "agm_date"), doc, at),
        warnings=warn,
    )
