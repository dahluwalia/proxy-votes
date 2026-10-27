"""One cited web-search call to the research model, and the check that keeps it honest.

Design choice: the model's JSON is untrusted. A fact survives only when it quotes a
page and cites a URL that the search returned in that same response; anything else
is dropped to unknown. Search errors (the API returns them inside a 200) and
outages become problems for the caller to turn into review items, and are never
cached, so the next run retries. Good answers are cached for 7 days through
core.cache, slimmed to what validation and citation need.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pennant import calllog, logs
from pennant.classify import backends
from pennant.core.cache import WEB_TTL, cache_key, cached, put_cache
from pennant.core.config import FIXTURES_DIR, Settings
from pennant.core.models import Evidence, RunContext

WEB_FIXTURES = FIXTURES_DIR / "web"
BENIGN_ERRORS = {"max_uses_exceeded"}  # the model still answers with what it found
log = logs.get("research")
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def call(settings: Settings, prompt: str, meta: dict[str, Any],
         domains: list[str] | None = None) -> dict[str, Any]:
    """One research call to the provider LLM_MODEL names (classify/backends.py), stamped
    with the question (`meta`) and the time, as the cache stores it. `domains` limits the
    search to those websites."""
    search, model = backends.provider(settings.llm_model)
    return {**meta, **search(settings, model, prompt, domains),
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds")}


def search_errors(payload: dict[str, Any]) -> list[str]:
    return [b["error_code"] for b in payload.get("content", [])
            if b.get("type") == "web_search_tool_result" and "error_code" in b
            and b["error_code"] not in BENIGN_ERRORS]


def parse_answer(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The last JSON object in the final text (fenced if possible), or None."""
    text = "\n".join(b["text"] for b in payload.get("content", []) if b.get("type") == "text")
    bare = [text[text.find("{"):text.rfind("}") + 1]] if "{" in text else []
    found = _FENCE.findall(text) or bare
    for candidate in reversed(found):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


class _NotCacheable(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


def ask(prompt_version: str, key_inputs: dict[str, str], prompt: Callable[[], str],
        ctx: RunContext, kind: str, domains: list[str] | None = None,
        ) -> tuple[dict[str, Any] | None, list[str]]:
    """(payload, problems). None means nothing usable: an offline miss or a failed search.
    A missing or rejected key, or a disabled web search tool, raises: expert must fix those."""
    meta = {"kind": kind, "prompt_version": prompt_version, "inputs": key_inputs}
    live: list[bool] = []
    key = cache_key(prompt_version, ctx.settings.llm_model, key_inputs)

    def fetch() -> dict[str, Any]:
        live.append(True)
        text, start = prompt(), time.monotonic()
        try:
            payload = call(ctx.settings, text, meta, domains)
        except Exception as e:  # recorded, then handled below or raised to expert as before
            outcome = "unavailable" if isinstance(e, backends.ModelUnavailable) else "error"
            _record(ctx, meta, text, None, outcome, f"{type(e).__name__}: {e}", start, key,
                    domains)
            raise
        errors = search_errors(payload)
        bad = (f"web search error: {', '.join(errors)}" if errors else
               "no JSON answer" if parse_answer(payload) is None else None)
        _record(ctx, meta, text, payload, "rejected" if bad else "ok", bad, start, key, domains)
        if bad:
            raise _NotCacheable(payload)
        return payload

    try:
        payload = cached(ctx.conn, "web", key, fetch, ttl=WEB_TTL, offline=ctx.offline)
    except _NotCacheable as e:
        payload = e.payload
    except backends.ModelUnavailable as e:
        log.warning("%s %s: research model unavailable: %s", kind, key_inputs, e)
        return None, [f"web research unavailable ({e})"]
    if payload is None:
        return None, ["no cached web research, and running offline"]
    logs.ai_usage(kind, payload.get("usage"), live=bool(live))
    if live:
        u = payload.get("usage") or {}
        log.info("%s %s: asked %s: %s in / %s out tokens, %s searches", kind, key_inputs,
                 payload.get("model"), u.get("input_tokens"), u.get("output_tokens"),
                 u.get("web_search_requests"))
    problem = None
    if errors := search_errors(payload):
        problem = f"web search error: {', '.join(errors)}"
    elif parse_answer(payload) is None:
        problem = f"no JSON answer (stop_reason {payload.get('stop_reason')})"
    if problem:
        log.warning("%s %s: %s", kind, key_inputs, problem)
        return None, [problem]
    return payload, []


def _record(ctx: RunContext, meta: dict[str, Any], prompt: str, payload: dict[str, Any] | None,
            outcome: str, error: str | None, start: float, key: str,
            domains: list[str] | None) -> None:
    """Append this live call to model_calls (calllog.py), with what validation keeps."""
    validation = None
    if payload is not None:
        values, _, rejected = validate(payload, domains[0] if domains else None)
        validation = {"kept": sorted(values), "rejected": rejected,
                      "answer": parse_answer(payload), "domains": domains}
    response = None if payload is None else {
        k: payload.get(k) for k in ("model", "stop_reason", "content")}
    calllog.record(ctx.conn, run_id=ctx.run_id, provider=ctx.settings.llm_model.rpartition(":")[0]
                   or backends.DEFAULT_PROVIDER, kind=meta["kind"],
                   prompt_version=meta["prompt_version"],
                   model=(payload or {}).get("model") or ctx.settings.llm_model,
                   inputs=meta["inputs"], prompt=prompt, response=response, outcome=outcome,
                   error=error, latency_s=time.monotonic() - start,
                   usage=(payload or {}).get("usage"), validation=validation, cache_key=key)


# --- validation ----------------------------------------------------------------


def host(url: str) -> str:
    """'https://www.Example.co.uk/board' -> 'example.co.uk'."""
    h = urlsplit(url.strip() if "//" in url else "//" + url.strip()).hostname or ""
    return h.removeprefix("www.").lower()


def on_site(url: str, domain: str) -> bool:
    """The URL is on `domain` or one of its subdomains."""
    h = host(url)
    return bool(domain) and (h == domain or h.endswith("." + domain))


def _norm_url(url: str) -> str:
    url = url.strip().split("#")[0].rstrip("/")
    return re.sub(r"^https?://(?:www\.)?", "", url, flags=re.I).lower()


def returned_sources(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalised URL -> search result, for every result the search returned."""
    return {_norm_url(r["url"]): r for b in payload.get("content", [])
            if b.get("type") == "web_search_tool_result" for r in b.get("results", [])}


def _evidence(raw: dict[str, Any], sources: dict[str, Any], fetched: str | None,
              domain: str | None = None) -> Evidence | None:
    url, quote = raw.get("url"), raw.get("quote")
    src = sources.get(_norm_url(url)) if isinstance(url, str) and url else None
    if src is None or not isinstance(quote, str) or not quote.strip():
        return None
    if domain and not on_site(src["url"], domain):
        return None
    return Evidence(source="web", ref=src["url"], quote=quote.strip()[:400],
                    title=src.get("title"), fetched_at=fetched)


def _unknown(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "unknown", "null"))


def validate(
    payload: dict[str, Any] | None, domain: str | None = None,
) -> tuple[dict[str, Any], dict[str, Evidence], list[str]]:
    """(values, evidence, rejected) per answer field; `events` become `events[i]`. A value
    survives only with a quote and a URL the search returned in this same response, and,
    with `domain`, only from a page on that website."""
    answer = parse_answer(payload) if payload else None
    if not payload or not answer:
        return {}, {}, []
    sources, fetched = returned_sources(payload), payload.get("fetched_at")
    values: dict[str, Any] = {}
    evidence: dict[str, Evidence] = {}
    rejected: list[str] = []
    items = [(k, v) for k, v in answer.items() if isinstance(v, dict)]
    items += [(f"events[{i}]", {**e, "value": e}) for i, e in enumerate(answer.get("events") or [])
              if isinstance(e, dict)]
    for name, raw in items:
        if _unknown(raw.get("value")):
            continue
        ev = _evidence(raw, sources, fetched, domain)
        if ev is None:
            off = domain and isinstance(raw.get("url"), str) and not on_site(raw["url"], domain)
            rejected.append(f"{name} cites {raw.get('url')!r}, not on {domain}" if off
                            else f"{name} cites {raw.get('url')!r}, not a returned search result"
                            if raw.get("quote") else f"{name} has no quote")
            continue
        values[name], evidence[name] = raw["value"], ev
    return values, evidence, rejected


# --- fixtures ------------------------------------------------------------------


def load_fixtures(conn: Any, directory: Path = WEB_FIXTURES) -> int:
    """Seed the web cache from recorded fixtures (offline tests, demos); returns how many."""
    files = sorted(directory.glob("*.json"))
    for f in files:
        fx = json.loads(f.read_text())
        put_cache(conn, "web", fx["key"], fx["payload"],
                  datetime.fromisoformat(fx["payload"]["fetched_at"]))
    return len(files)


def export_fixtures(conn: Any, directory: Path = WEB_FIXTURES) -> list[Path]:
    """Write every cached web answer to a fixture file named after its inputs."""
    directory.mkdir(parents=True, exist_ok=True)
    latest: dict[str, tuple[str, dict[str, Any]]] = {}
    rows = conn.execute("SELECT key, payload FROM cache WHERE namespace = 'web' ORDER BY id")
    for key, raw in rows:
        payload = json.loads(raw)
        stem = "__".join([payload["kind"], *payload["inputs"].values()])
        latest[re.sub(r"[^a-z0-9_.-]+", "-", stem.lower())] = (key, payload)
    written = []
    for stem, (key, payload) in sorted(latest.items()):
        path = directory / f"{stem}.json"
        path.write_text(json.dumps({"key": key, "payload": payload}, indent=1) + "\n")
        written.append(path)
    return written
