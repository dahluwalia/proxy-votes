"""M1.1: the LlamaExtract job, against a mocked response (no network, no credits)."""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from types import SimpleNamespace as NS

import pytest

from pennant.core.cache import get_cache
from pennant.core.config import NOTICES_DIR
from pennant.extract import llamaextract as le
from pennant.extract.schema import DATA_SCHEMA, parse_job, schema_sha

TESCO = NOTICES_DIR / "Tesco_2026_Notice_of_AGM.pdf"
SILVER_LINE = "Chair: Barratt Redrow plc and ICE Clear Europe"


def cite(page, text):
    return {"citation": [{"page": page, "matching_text": text}]}


def mock_job(status="COMPLETED"):
    """A trimmed Tesco response in the ExtractV2Job shape, citations included."""
    appt = {"role": "Chair", "tense": "current", "section": "current_appointments",
            "quote": SILVER_LINE}
    return {
        "id": "ext-mock-1", "status": status, "file_input": "dfl-mock", "project_id": "prj",
        "created_at": "2026-10-26T12:00:00Z", "updated_at": "2026-10-26T12:03:00Z",
        "extract_result": {
            "company_name": "Tesco PLC", "company_number": "00445790", "agm_date": "2026-06-18",
            "directors": [
                {"resolution_no": 12, "name": "Imran Nawaz", "coverage": "structured",
                 "role_at_company": "Chief Financial Officer", "appointments": []},
                {"resolution_no": 13, "name": "Caroline Silver", "coverage": "structured",
                 "role_at_company": "Independent Non-executive Director", "appointments": [
                     {"organisation": "Barratt Redrow plc", **appt},
                     {"organisation": "ICE Clear Europe", **appt}]},
            ],
        },
        "extract_metadata": {"field_metadata": {"document_metadata": {
            "company_name": cite(1, "Tesco PLC"),
            "company_number": cite(5, "Registered in England and Wales: 00445790"),
            "agm_date": cite(1, "Thursday, 18 June 2026"),
            "directors": [
                {"name": cite(8, "Imran Nawaz,"), "resolution_no": cite(4, "12.  Imran Nawaz")},
                {"name": cite(10, "Caroline Silver,"), "appointments": [
                    {"quote": cite(10, SILVER_LINE),
                     "organisation": cite(10, "Barratt Redrow plc")},
                    {"quote": cite(10, SILVER_LINE)}]},
            ],
        }}},
        "usage": {"credits": 400.0},
    }


class FakeClient:
    """Stands in for llama_cloud.LlamaCloud: files.create, extract.create/wait/get."""

    def __init__(self, job=None, status="COMPLETED", fail_upload=False):
        self.job = job or mock_job(status)
        self.calls: list[tuple] = []
        self.fail_upload = fail_upload
        self.files = NS(create=self._upload)
        self.extract = NS(create=self._create, wait_for_completion=self._wait, get=self._get)

    def _upload(self, file, purpose):
        self.calls.append(("upload", file[0], purpose))
        if self.fail_upload:
            raise ConnectionError("network down")
        return NS(id="dfl-mock")

    def _create(self, file_input, configuration):
        self.calls.append(("create", file_input, configuration))
        return NS(id=self.job["id"])

    def _wait(self, job_id, **_):
        return NS(id=job_id, status=self.job["status"], error_message="boom")

    def _get(self, job_id, expand):
        self.calls.append(("get", job_id, tuple(expand)))
        return NS(model_dump=lambda mode: copy.deepcopy(self.job))


@pytest.fixture
def live_ctx(ctx):
    return dataclasses.replace(ctx, offline=False)


def test_config_is_pinned_and_prompt_states_the_rules():
    assert le.CONFIG["tier"] == "agentic"
    assert le.CONFIG["cite_sources"] is True
    assert le.CONFIG["extraction_target"] == "per_doc"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", le.CONFIG["version"])
    assert le.job_configuration()["data_schema"] == DATA_SCHEMA
    prompt = le.SYSTEM_PROMPT
    for phrase in ("election or re-election", "company secretaries", "is two appointments",
                   "previously, formerly, until, retired, was", "Key current appointments",
                   "structured if there is a current-appointments list", "Never paraphrase"):
        assert phrase in prompt


def test_schema_asks_for_every_field():
    director = DATA_SCHEMA["properties"]["directors"]["items"]
    appointment = director["properties"]["appointments"]["items"]
    assert set(DATA_SCHEMA["required"]) == {"company_name", "company_number", "agm_date",
                                            "directors"}
    assert set(director["required"]) == {"resolution_no", "name", "role_at_company",
                                         "coverage", "appointments"}
    assert set(appointment["required"]) == {"organisation", "role", "tense", "section", "quote"}
    assert appointment["properties"]["tense"]["enum"] == ["current", "past", "future"]


def test_citations_map_onto_their_fields():
    ex = parse_job(mock_job(), TESCO.name, "sha")
    assert (ex.company_name, ex.company_number, ex.agm_date) == ("Tesco PLC", "00445790",
                                                                 "2026-06-18")
    assert ex.evidence["company_number"][0].ref == "Tesco_2026_Notice_of_AGM.pdf#page=5"
    nawaz, silver = ex.directors
    assert nawaz.coverage == "structured" and nawaz.appointments == []
    assert nawaz.cited_pages() == [8, 4]
    assert [a.organisation for a in silver.appointments] == ["Barratt Redrow plc",
                                                            "ICE Clear Europe"]
    barratt = silver.appointments[0]
    assert barratt.evidence["quote"][0].source == "notice"
    assert barratt.evidence["quote"][0].quote == SILVER_LINE
    assert set(barratt.evidence) == {"quote", "organisation"}
    assert barratt.cited_pages() == [10]
    assert ex.warnings == []


def test_live_call_happens_once_then_cache_hits(live_ctx, tmp_path):
    client = FakeClient()
    first = le.extract_notice(TESCO, live_ctx, client=client, fixtures_dir=tmp_path)
    second = le.extract_notice(TESCO, live_ctx, client=client, fixtures_dir=tmp_path)
    assert first == second and len(first.directors) == 2
    kinds = [c[0] for c in client.calls]
    assert kinds == ["upload", "create", "get"]
    assert client.calls[1][2] == le.job_configuration()
    assert "extract_metadata" in client.calls[2][2]


def test_offline_miss_returns_none(ctx, tmp_path):
    client = FakeClient()
    assert le.extract_notice(TESCO, ctx, client=client, fixtures_dir=tmp_path) is None
    assert client.calls == []


def test_recording_seeds_the_cache_offline(ctx, tmp_path):
    path = le.write_fixture(TESCO, mock_job(), tmp_path)
    record = json.loads(path.read_text())
    assert record["cache_key"] == le.notice_key(TESCO.read_bytes())
    ex = le.extract_notice(TESCO, ctx, fixtures_dir=tmp_path)
    assert ex is not None and ex.job_id == "ext-mock-1"
    _, fetched_at = get_cache(ctx.conn, "llamaextract", record["cache_key"])
    assert fetched_at.isoformat(timespec="seconds") == record["recorded_at"]


def test_second_live_run_for_a_recorded_notice_is_refused(live_ctx, tmp_path):
    path = le.write_fixture(TESCO, mock_job(), tmp_path)
    record = json.loads(path.read_text())
    record["cache_key"] = "recorded-under-an-older-config"
    path.write_text(json.dumps(record))
    client = FakeClient()
    with pytest.raises(le.BudgetError, match="ask expert"):
        le.extract_notice(TESCO, live_ctx, client=client, fixtures_dir=tmp_path)
    assert client.calls == []
    le.extract_notice(TESCO, live_ctx, client=client, fixtures_dir=tmp_path, force=True)
    assert [c[0] for c in client.calls] == ["upload", "create", "get"]


@pytest.mark.parametrize("client", [FakeClient(status="FAILED"), FakeClient(fail_upload=True)])
def test_failures_raise_and_cache_nothing(live_ctx, tmp_path, client):
    with pytest.raises(le.ExtractError):
        le.extract_notice(TESCO, live_ctx, client=client, fixtures_dir=tmp_path)
    assert get_cache(live_ctx.conn, "llamaextract", le.notice_key(TESCO.read_bytes())) is None


def test_missing_key_fails_before_any_network(live_ctx, tmp_path):
    with pytest.raises(le.ExtractError, match="LLAMA_CLOUD_API_KEY"):
        le.extract_notice(TESCO, live_ctx, fixtures_dir=tmp_path)


def test_resume_existing_job_does_not_create_a_new_one(live_ctx, tmp_path):
    client = FakeClient()
    le.extract_notice(TESCO, live_ctx, client=client, fixtures_dir=tmp_path, job_id="ext-old")
    assert [c[0] for c in client.calls] == ["get"]


def test_odd_values_fail_closed_with_warnings():
    job = mock_job()
    nawaz, silver = job["extract_result"]["directors"]
    nawaz["resolution_no"] = "seven"
    nawaz["coverage"] = "partial"
    silver["resolution_no"] = "13"
    silver["appointments"][0]["tense"] = "formerly"
    job["extract_metadata"] = None
    ex = parse_job(job, TESCO.name, "sha")
    assert ex.directors[0].resolution_no is None
    assert ex.directors[0].coverage == "none"
    assert ex.directors[1].resolution_no == 13
    assert ex.directors[1].appointments[0].tense == "unclear"
    assert ex.directors[1].evidence == {}
    assert len(ex.warnings) == 3


def test_citation_shape_variants():
    job = mock_job()
    meta = job["extract_metadata"]["field_metadata"]["document_metadata"]
    meta["company_name"] = {"citations": [{"page_number": "2", "text": "Tesco PLC"},
                                          {"page": None, "matching_text": "x"}]}
    ex = parse_job(job, TESCO.name, "sha")
    assert [e.ref for e in ex.evidence["company_name"]] == ["Tesco_2026_Notice_of_AGM.pdf#page=2"]


def test_per_page_output_is_rejected():
    job = mock_job()
    job["extract_result"] = [job["extract_result"], job["extract_result"]]
    with pytest.raises(ValueError, match="single object"):
        parse_job(job, TESCO.name, "sha")


# --- Accept, on the five recordings (offline) --------------------------------

RECORDED = sorted(NOTICES_DIR.glob("*.pdf"))


@pytest.mark.parametrize("pdf", RECORDED, ids=lambda p: p.stem)
def test_recording_is_current_and_extracts_offline(ctx, pdf):
    record = le.load_fixture(pdf)
    assert record is not None, f"no recording for {pdf.name}"
    assert record["cache_key"] == le.notice_key(pdf.read_bytes())
    assert record["schema_sha"] == schema_sha(), "DATA_SCHEMA changed: bump SCHEMA_VERSION"
    assert record["response"]["configuration"]["cite_sources"] is True
    ex = le.extract_notice(pdf, ctx)
    assert ex is not None and ex.directors and ex.warnings == []


def recorded(ctx, name):
    pdf = next(p for p in RECORDED if p.name.startswith(name))
    return le.extract_notice(pdf, ctx)


def director(ex, name):
    return next(d for d in ex.directors if name in d.name)


def test_caroline_silver_has_seven_appointments(ctx):
    silver = director(recorded(ctx, "Tesco"), "Caroline Silver")
    assert len(silver.appointments) == 7
    roles = {(a.organisation, a.role) for a in silver.appointments}
    assert {("Barratt Redrow plc", "Chair"), ("ICE Clear Europe", "Chair")} <= roles
    assert all(a.cited_pages() == [10] for a in silver.appointments)


def test_compound_lists_are_split(ctx):
    tesco = recorded(ctx, "Tesco")
    assert len(director(tesco, "Melissa Bethell").appointments) == 6  # 4 NED + adviser + director
    assert len(director(tesco, "Karen Whitworth").appointments) == 3


def test_past_appointments_are_past(ctx):
    hodgson = director(recorded(ctx, "Severn"), "Christine Hodgson")
    lacroix = director(recorded(ctx, "Intertek"), "André Lacroix")
    assert {a.tense for a in hodgson.appointments if "Standard Chartered" in a.organisation} == {
        "past"}
    assert {a.tense for a in lacroix.appointments if "Reckitt" in a.organisation} == {"past"}


def test_all_nine_ms_directors_have_no_biography(ctx):
    ms = recorded(ctx, "Marks")
    assert len(ms.directors) == 9 and {d.coverage for d in ms.directors} == {"none"}
    assert all(d.appointments == [] for d in ms.directors)
