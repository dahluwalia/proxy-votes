"""Which model answers a research question. Every provider returns the same payload.

Design choice: research needs a model that searches the web and reports which pages its
search returned, because a fact counts only if it cites one of those pages
(websearch.validate). So a provider is one function, `(settings, model, prompt,
domains) -> payload`, and every provider returns this shape. `domains`, when given,
limits the search to those websites (a person's role is looked up only on the firm's own
site); a provider that can't enforce that must refuse rather than search wider:

    {"model": str, "stop_reason": str, "usage": {...},
     "content": [{"type": "server_tool_use", "query": str},          # a search it ran
                 {"type": "web_search_tool_result",
                  "results": [{"url", "title", "page_age"}]},        # what it returned
                 {"type": "web_search_tool_result", "error_code": str},  # or a failure
                 {"type": "text", "text": str}]}                     # the answer

The block names are Anthropic's, kept because the recorded answers use them. A
transient failure (network, rate limit, 5xx) raises ModelUnavailable, which the caller
turns into unknown plus a review item; anything else (a bad key, search disabled) stops
the run for expert to fix.

`LLM_MODEL` is "[provider:]model", e.g. "claude-sonnet-5-5" or
"anthropic:claude-sonnet-5-5"; the provider defaults to anthropic. To plug in another,
write its function, add it to PROVIDERS and set LLM_MODEL. The whole LLM_MODEL string
is in the cache key, so answers from different models never mix. Prompts are in this
package's prompts.md.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anthropic

from pennant.core.config import Settings

Payload = dict[str, Any]
Provider = Callable[[Settings, str, str, list[str] | None], Payload]

# The basic tool: each search's results go straight to the model, so every URL it can cite
# is one it read. (The dynamic-filtering variant re-ran failed searches, wasting max_uses.)
WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
DEFAULT_PROVIDER = "anthropic"


class ModelUnavailable(Exception):
    """A transient failure: the answer is unknown this run, and nothing is cached."""


def _slim(b: dict[str, Any]) -> dict[str, Any] | None:
    """Keep search queries, results and text; drop encrypted content and thinking."""
    t = b.get("type")
    if t == "web_search_tool_result":
        c = b.get("content")
        if isinstance(c, list):
            return {"type": t, "results": [{k: r.get(k) for k in ("url", "title", "page_age")}
                                           for r in c if r.get("url")]}
        return {"type": t, "error_code": (c or {}).get("error_code", "unknown")}
    if t == "server_tool_use" and b.get("name") == "web_search":
        return {"type": t, "query": (b.get("input") or {}).get("query")}
    if t == "text":
        return {"type": t, "text": b.get("text", "")}
    return None


def anthropic_search(settings: Settings, model: str, prompt: str,
                     domains: list[str] | None = None) -> Payload:
    """One Messages API call with the web search tool, resuming pause_turn at most twice.
    `domains` becomes the tool's allowed_domains (subdomains included)."""
    tool = {**WEB_SEARCH, "allowed_domains": domains} if domains else WEB_SEARCH
    client = anthropic.Anthropic(api_key=settings.require("ANTHROPIC_API_KEY"), max_retries=4)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    blocks: list[dict[str, Any]] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0}
    try:
        for _ in range(3):
            r = client.beta.messages.create(  # type: ignore[call-overload]
                model=model, max_tokens=16000, messages=messages, tools=[tool],
                betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            blocks += [b.model_dump(mode="json") for b in r.content]
            u = r.usage.model_dump()
            usage["input_tokens"] += u.get("input_tokens") or 0
            usage["output_tokens"] += u.get("output_tokens") or 0
            searches = (u.get("server_tool_use") or {}).get("web_search_requests")
            usage["web_search_requests"] += searches or 0
            if r.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": r.content})
    except (anthropic.APIConnectionError, anthropic.RateLimitError) as e:
        raise ModelUnavailable(type(e).__name__) from e
    except anthropic.APIStatusError as e:
        if e.status_code < 500:
            raise  # a bad key or a disabled tool: expert must fix it
        raise ModelUnavailable(f"HTTP {e.status_code}") from e
    return {"model": r.model, "stop_reason": r.stop_reason, "usage": usage,
            "content": [s for b in blocks if (s := _slim(b))]}


PROVIDERS: dict[str, Provider] = {"anthropic": anthropic_search}


def provider(llm_model: str) -> tuple[Provider, str]:
    """(provider function, model name) for an LLM_MODEL setting. An unknown provider is
    an error, never a silent fallback to another model."""
    name, _, model = llm_model.rpartition(":")
    name = name or DEFAULT_PROVIDER
    if name not in PROVIDERS:
        raise ValueError(f"LLM_MODEL={llm_model!r}: no research provider {name!r}; "
                         f"choose one of {sorted(PROVIDERS)}")
    return PROVIDERS[name], model
