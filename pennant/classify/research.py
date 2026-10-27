"""One research question for a model with web search: what is this organisation?

Design choice: this question is about organisations only: whether its shares were
listed on the AGM date, on which market, and whether it is a subsidiary (and of whom).
A person's role is asked separately (positions.py), and only of the firm's own website.
It runs only where rules and CH leave the answer open. The model is whichever LLM_MODEL
names (classify/backends.py); the prompt is in prompts.md. Answers pass through
websearch.validate, so every surviving fact cites a page the search returned. Facts map
onto OrgFacts as yes / no / unknown; anything unsourced or failed stays unknown and
leaves a review item. Cache keys use normalised inputs (org_key, AGM date), so a
re-extracted spelling still hits the recorded answer.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from pennant.classify.websearch import ask, validate
from pennant.core import db
from pennant.core.cache import cache_key, get_cache
from pennant.core.config import Policy
from pennant.core.models import (
    OrgFacts,
    ReviewItem,
    RunContext,
    StageReport,
)
from pennant.core.tri import NO, UNKNOWN, YES, Tri, t_and, t_or, to_tri
from pennant.prompts import prompt

PROMPTS = Path(__file__).with_name("prompts.md")
PROMPT_VERSION = {"org": "org_v1", "site": "site_v1", "position": "position_v2"}
SEGMENT_UNKNOWN = "segment unknown"  # in OrgFacts.market: main market or AIM not sourced
NOT_COMPANY_TYPES = {"charity", "public body", "education", "partnership"}
# Legal-form words that don't tell two organisations apart.
_LEGAL = {"plc", "limited", "ltd", "inc", "incorporated", "corp", "corporation", "llp", "llc",
          "nv", "sa", "ag", "se", "asa", "oyj", "spa", "bhd", "pte", "cyfyngedig", "ccc"}


def org_tokens(name: str) -> list[str]:
    """An org name's words with accents, case, '&', punctuation, a leading 'the' and
    trailing legal suffixes folded away: every org join key is built from these."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    folded = re.sub(r"public limited company|public company limited", "plc",
                    folded.replace(".", ""))
    toks = re.sub(r"[^a-z0-9]+", " ", folded.replace("&", " and ")).split()
    toks = toks[1:] if toks[:1] == ["the"] else toks
    while len(toks) > 1 and toks[-1] in _LEGAL:
        toks.pop()
    return toks


def org_key(name: str) -> str:
    """'Barratt Redrow plc' -> 'barratt-redrow'; 'The Magnum Ice Cream Company N.V.' ->
    'magnum-ice-cream-company'. Part of the web cache key: changing it re-asks everything."""
    return "-".join(org_tokens(name)) or "unknown"


def render(kind: str, **values: str) -> str:
    text = prompt(PROMPTS, PROMPT_VERSION[kind])
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


def _name(v: Any) -> str | None:
    """A bare name: commentary after '(' or ';' is cut ('Severn Trent Plc (ultimate parent)')."""
    s = re.split(r"\s*[(;]", v, maxsplit=1)[0].strip(" .,") if isinstance(v, str) else ""
    return s or None


def _date(v: Any) -> date | None:
    """'2026-06-29' or '2026-06' (the 1st) -> a date; anything else -> None."""
    s = str(v or "")
    for text in (s[:10], s[:7] + "-01"):
        try:
            return date.fromisoformat(text)
        except ValueError:
            continue
    return None


def _tri(v: Any) -> Tri:
    try:
        return to_tri(v)
    except ValueError:
        return UNKNOWN


def _review(ctx: RunContext, subject: str, problems: list[str], rejected: list[str],
            command: str) -> None:
    items = [ReviewItem(kind="web_research_failed", subject=subject, detail=p,
                        suggested_command=command) for p in problems]
    if rejected:
        items.append(ReviewItem(kind="web_unsourced", subject=subject, suggested_command=command,
                                detail="dropped unsourced answers: " + "; ".join(rejected)))
    if items:
        db.put_outputs(ctx.conn, ctx.run_id, "review", items)


def research_org(name: str, as_of: date | str, ctx: RunContext,
                 key: str | None = None) -> OrgFacts:
    """The organisation's listing and parent on `as_of`. `equity_listing` is shares only;
    `listed` adds a debt listing the search found, so a bond-only issuer is listed yes,
    equity no. Sources rarely say a company has no bonds, so debt counts only when found."""
    key = key or org_key(name)  # the caller's join key; the cache always uses org_key(name)
    values = {"org": name, "as_of": str(as_of), "today": date.today().isoformat()}
    payload, problems = ask(PROMPT_VERSION["org"], {"org_key": org_key(name), "as_of": str(as_of)},
                            lambda: render("org", **values), ctx, kind="org")
    v, ev, rejected = validate(payload)
    equity, debt = _tri(v.get("equity_listed")), _tri(v.get("debt_listed"))
    segment, exchange = str(v.get("market_segment", "")).lower(), _name(v.get("exchange"))
    market = None
    if equity == YES:  # a segment not sourced as main / AIM / growth stays unknown (fail closed)
        market = {"main": exchange or "exchange unknown", "aim": "AIM",
                  "growth": f"{exchange or 'unknown exchange'} growth market"}.get(
            segment, f"{exchange or 'exchange unknown'} ({SEGMENT_UNKNOWN})")
        market += f" ({_name(v['ticker'])})" if _name(v.get("ticker")) else ""
    elif debt == YES:
        market = f"{exchange or 'exchange unknown'} (debt only)"
    parent = _name(v.get("parent_name")) if _tri(v.get("is_subsidiary")) == YES else None
    events = [f"{e.get('date')}: {e.get('event')}" for k, e in v.items() if k.startswith("events[")]
    facts = OrgFacts(
        org_key=key, canonical_name=_name(v.get("legal_name")) or name,
        # Raw facts: equity no + debt unknown is unknown here; seat_listed then reads bonds
        # never found as not listed, so the seat's `listed` is no.
        listed=t_or(equity, debt),
        org_type=v.get("org_type"), market=market, equity_listing=equity,
        parent_name=parent if parent and org_key(parent) != key else None, events=events,
        evidence=list({(e.ref, e.quote): e for e in ev.values()}.values()))
    _review(ctx, key, problems, rejected, f'pennant set-fact {key} listed <yes|no> --reason "..."')
    return facts


def cached_parent(name: str, as_of: date | str, ctx: RunContext) -> str | None:
    """The parent from an org answer already in the cache, if any. Never calls out and
    writes no review item: rules settled the listing, this only links the group (H8)."""
    key = cache_key(PROMPT_VERSION["org"], ctx.settings.llm_model,
                    {"org_key": org_key(name), "as_of": str(as_of)})
    hit = get_cache(ctx.conn, "web", key)
    v = validate(hit[0])[0] if hit else {}
    return _name(v.get("parent_name")) if _tri(v.get("is_subsidiary")) == YES else None


def is_growth_market(market: str | None) -> bool:
    return bool(market and re.search(r"\bAIM\b|growth", market, re.I))


def segment_unknown(market: str | None) -> bool:
    """No market, or research could not source the segment (main market vs AIM)."""
    return not market or SEGMENT_UNKNOWN in market


def not_a_company(org_type: str | None) -> bool:
    """Charities, public bodies, education bodies and partnerships never count as listed."""
    return re.sub(r"[\s_-]+", " ", (org_type or "").strip().lower()) in NOT_COMPANY_TYPES


def seat_listed(org: OrgFacts | None, policy: Policy) -> Tri:
    """Listed for the policy: equity on a known main market counts; AIM and growth markets
    follow aim_counts_as_listed, and a debt-only listing follows debt_only_listing_counts.
    Equity on an unknown segment could be AIM: yes only if AIM counts, else unknown.
    A debt listing is one the search found (listed yes, equity not yes): sources rarely say
    a company has no bonds, so a company whose shares are not listed and whose bonds were
    never found is a private company here, not an unknown. Non-companies are never listed."""
    if org is None:
        return UNKNOWN
    if not_a_company(org.org_type):
        return NO
    equity = org.equity_listing
    if equity == YES and is_growth_market(org.market):
        equity = policy.aim_counts_as_listed
    elif equity == YES and segment_unknown(org.market):
        equity = YES if policy.aim_counts_as_listed == YES else UNKNOWN
    # Bonds were found when listed (shares or bonds) is yes but the shares aren't. They count
    # under the policy whatever is known about the shares: shares unknown + bonds is still yes.
    bonds = YES if org.listed == YES and org.equity_listing != YES else NO
    return t_or(equity, t_and(bonds, policy.debt_only_listing_counts))


def run(ctx: RunContext) -> StageReport:
    """The classify stage (rules, then research). It lives in classify/stage.py."""
    from pennant.classify.stage import run as run_stage

    return run_stage(ctx)
