"""One LlamaExtract job per notice: meeting, roster and appointments, with citations.

Design choice: the job runs once per notice and its raw response is the record.
`extract_notice()` goes through `core.cache`, keyed on (PDF bytes, schema
version, config). The committed fixtures in tests/fixtures/llamaextract/ are the
recordings of those single live runs, and they seed the cache, so a fresh
database never spends credits. If a notice already has a fixture but the config
has changed, the live call is refused: a second run per notice costs credits,
so expert decides, then re-records with `--force`.

Record (live, needs LLAMA_CLOUD_API_KEY):
    .venv/bin/python -m pennant.extract.llamaextract record [--force] [pdf ...]
Recover a job that outlived polling, without starting a new one:
    .venv/bin/python -m pennant.extract.llamaextract record --job <job_id> <pdf>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pennant import calllog, logs
from pennant.core import db
from pennant.core.cache import cache_key, cached, get_cache, put_cache
from pennant.core.config import (
    FIXTURES_DIR,
    NOTICES_DIR,
    Settings,
    load_policy,
    load_settings,
    notices,
)
from pennant.core.models import RunContext
from pennant.extract.schema import DATA_SCHEMA, SCHEMA_VERSION, NoticeExtract, parse_job, schema_sha
from pennant.prompts import prompt

log = logs.get("extract")

FIXTURES = FIXTURES_DIR / "llamaextract"
NAMESPACE = "llamaextract"
EXTRACT_VERSION = "2026-10-26"  # pinned: the latest extract release on or before this date
POLL_TIMEOUT_S = 1800

SYSTEM_PROMPT = prompt(Path(__file__).with_name("prompts.md"), "system_v1")

# Everything sent with the job except the data schema (that is SCHEMA_VERSION).
CONFIG: dict[str, Any] = {
    "extraction_target": "per_doc",
    "tier": "agentic",
    "cite_sources": True,
    "version": EXTRACT_VERSION,
    "system_prompt": SYSTEM_PROMPT,
}


class BudgetError(RuntimeError):
    """A live run would be the second for a notice that already has a recording."""


class ExtractError(RuntimeError):
    """The LlamaExtract job failed, was cancelled or could not be fetched."""


def job_configuration() -> dict[str, Any]:
    return {**CONFIG, "data_schema": DATA_SCHEMA}


def notice_key(pdf_bytes: bytes) -> str:
    return cache_key(pdf_bytes, SCHEMA_VERSION, CONFIG)


def fixture_path(pdf_path: str | Path, fixtures_dir: Path = FIXTURES) -> Path:
    return fixtures_dir / f"{Path(pdf_path).stem}.json"


def load_fixture(pdf_path: str | Path, fixtures_dir: Path = FIXTURES) -> dict[str, Any] | None:
    path = fixture_path(pdf_path, fixtures_dir)
    return json.loads(path.read_text()) if path.exists() else None


def write_fixture(
    pdf_path: str | Path, raw: dict[str, Any], fixtures_dir: Path = FIXTURES
) -> Path:
    pdf_bytes = Path(pdf_path).read_bytes()
    record = {
        "doc": Path(pdf_path).name,
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "schema_version": SCHEMA_VERSION,
        "schema_sha": schema_sha(),
        "config": CONFIG,
        "cache_key": notice_key(pdf_bytes),
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "response": raw,
    }
    path = fixture_path(pdf_path, fixtures_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return path


def seed_from_fixture(conn, pdf_path: str | Path, fixtures_dir: Path = FIXTURES) -> bool:
    """Copy a matching recording into the cache (once). True if the cache now has it."""
    key = notice_key(Path(pdf_path).read_bytes())
    if get_cache(conn, NAMESPACE, key) is not None:
        return True
    record = load_fixture(pdf_path, fixtures_dir)
    if record is None or record.get("cache_key") != key:
        return False
    put_cache(conn, NAMESPACE, key, record["response"],
              fetched_at=datetime.fromisoformat(record["recorded_at"]))
    return True


def _client(settings: Settings, client: Any = None) -> Any:
    if client is not None:
        return client
    from llama_cloud import LlamaCloud

    return LlamaCloud(api_key=settings.require("LLAMA_CLOUD_API_KEY"))


def fetch_live(
    pdf_path: str | Path, settings: Settings, client: Any = None, job_id: str | None = None
) -> dict[str, Any]:
    """Upload the PDF, run one extract job (or resume `job_id`) and return the raw job
    JSON with citations. Raises ExtractError on any failure, so nothing is cached."""
    path = Path(pdf_path)
    try:
        api = _client(settings, client)
        if job_id is None:
            uploaded = api.files.create(
                file=(path.name, path.read_bytes(), "application/pdf"), purpose="extract"
            )
            job = api.extract.create(file_input=uploaded.id, configuration=job_configuration())
            job_id = job.id
            print(f"{path.name}: LlamaExtract job {job_id} created", file=sys.stderr)
        job = api.extract.wait_for_completion(job_id, timeout=POLL_TIMEOUT_S, max_interval=10)
        if job.status != "COMPLETED":
            raise ExtractError(f"job {job_id} ended {job.status}: {job.error_message}")
        full = api.extract.get(job_id, expand=["extract_metadata", "usage", "configuration"])
        return full.model_dump(mode="json")
    except ExtractError:
        raise
    except Exception as exc:  # SDK, network or polling errors: fail closed, cache nothing
        raise ExtractError(f"{path.name}: LlamaExtract job {job_id or '(not created)'} "
                           f"failed: {exc}") from exc


def extract_notice(
    pdf_path: str | Path,
    ctx: RunContext,
    *,
    client: Any = None,
    fixtures_dir: Path = FIXTURES,
    force: bool = False,
    job_id: str | None = None,
) -> NoticeExtract | None:
    """The notice's extract, from cache, a committed recording or (once) a live job.
    None when offline with nothing stored: callers treat that as unknown."""
    path = Path(pdf_path)
    pdf_bytes = path.read_bytes()
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    seed_from_fixture(ctx.conn, path, fixtures_dir)

    live: list[bool] = []

    def fetch() -> dict[str, Any]:
        live.append(True)
        if fixture_path(path, fixtures_dir).exists() and not force:
            raise BudgetError(
                f"{path.name} already has a recorded LlamaExtract run, but the schema or "
                "config has changed since. A second live run costs credits: ask expert, "
                "then re-record with --force."
            )
        config = {k: v for k, v in CONFIG.items() if k != "system_prompt"}
        start = time.monotonic()
        inputs = {"pdf": path.name, "sha256": sha, "schema_version": SCHEMA_VERSION, **config}
        common = dict(run_id=ctx.run_id, provider="llamaextract", kind="extract",
                      prompt_version=f"system_v1/{SCHEMA_VERSION}",
                      model=f"{CONFIG.get('tier')}/{CONFIG.get('version')}", inputs=inputs,
                      prompt=SYSTEM_PROMPT, cache_key=notice_key(pdf_bytes))
        try:
            raw = fetch_live(path, ctx.settings, client, job_id)
        except Exception as e:
            calllog.record(ctx.conn, **common, outcome="error", error=f"{type(e).__name__}: {e}",
                           latency_s=time.monotonic() - start)
            raise
        calllog.record(ctx.conn, **common, outcome="ok", response=raw, usage=raw.get("usage"),
                       latency_s=time.monotonic() - start)
        return raw

    raw = cached(ctx.conn, NAMESPACE, notice_key(pdf_bytes), fetch, offline=ctx.offline)
    if raw is None:
        log.warning("%s: no extract (offline and nothing recorded)", path.name)
        return None
    logs.extract_usage(raw.get("usage"), live=bool(live))
    log.info("%s: LlamaExtract %s, %s credits", path.name, "job run" if live else "from cache",
             (raw.get("usage") or {}).get("credits", "unknown"))
    return parse_job(raw, path.name, sha)


# --- recording ---------------------------------------------------------------


def record(pdf_paths: list[Path], force: bool = False, job_id: str | None = None) -> None:
    """Run each notice live once (through the cache in PENNANT_DB) and write its fixture."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    ctx = RunContext(conn, "record", settings, load_policy(), offline=False)
    for pdf in pdf_paths:
        if seed_from_fixture(conn, pdf) and not force:
            print(f"{pdf.name}: recording matches the current config; skipped")
            continue
        extract = extract_notice(pdf, ctx, force=force, job_id=job_id)
        raw = get_cache(conn, NAMESPACE, notice_key(pdf.read_bytes()))
        if extract is None or raw is None:
            raise ExtractError(f"{pdf.name}: nothing recorded")
        out = write_fixture(pdf, raw[0])
        credits = (raw[0].get("usage") or {}).get("credits")
        print(f"{pdf.name}: {len(extract.directors)} directors, "
              f"{sum(len(d.appointments) for d in extract.directors)} appointments, "
              f"{credits} credits -> {out}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pennant.extract.llamaextract")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="run notices live once and write fixtures")
    rec.add_argument("pdfs", nargs="*", type=Path)
    rec.add_argument("--force", action="store_true", help="re-run a notice that has a fixture")
    rec.add_argument("--job", help="fetch this existing job instead of creating one")
    args = parser.parse_args(argv)
    pdfs = args.pdfs or [NOTICES_DIR / name for name in notices().values()]
    if args.job and len(pdfs) != 1:
        parser.error("--job needs exactly one pdf")
    record(pdfs, force=args.force, job_id=args.job)


if __name__ == "__main__":
    main()
