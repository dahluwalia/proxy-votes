# Technical guide

For developers and reviewers: how the pipeline is built, where each piece lives, and how to
test and extend it. Running it is in the [user guide](user-guide.md); the
[pipeline diagrams](pipeline.html) draw each stage. The build spec is
[`CLAUDE.md`](../CLAUDE.md), and each module's docstring states its design choice.

- [Architecture](#architecture)
- [Data model and database](#data-model-and-database)
- [Stage interface](#stage-interface)
- [Caching](#caching)
- [Logging and the model call record](#logging-and-the-model-call-record)
- [Testing](#testing)
- [Extending it](#extending-it)
- [Recording fixtures](#recording-fixtures-live-once)
- [Layout](#layout)
- [Further reading](#further-reading)

## Architecture

Six stages run in order. Each reads the same run's earlier outputs and writes its own
rows to `run_outputs`, so every run is a snapshot and any two can be diffed.

| Stage | Module | What it does | External calls |
| --- | --- | --- | --- |
| extract | `extract/validate.py` | reads each notice; checks the company number, AGM date and resolution numbers against the PDF text; grounds every quote on its cited page | extract model (LlamaExtract), once per notice |
| ch | `ch/client.py` | each issuer's officer list, then the full appointments of every director in office on the AGM date | Companies House |
| dedup | `dedup/cluster.py` | anchors each name among the issuer's same-surname directors (3-gram TF-IDF on first names; a former director only if no one in office matches), links other CH records by date of birth, clusters under analyst decisions | CH officer search |
| classify | `classify/stage.py` | role rules from the notice's titles; CH company type rules out listing; web research on organisations (listing, market, parent); roles still unknown asked of the firm's own website (`classify/positions.py`) | research model (Claude with web search) |
| score | `policy/stage.py` | reconciles notice seats with CH on the AGM date; points interval, executive rule, vote | none |
| evidence | `evidence/freshness.py` | dates every CH and web citation by when it was last confirmed; flags one checked before the day its vote needs (`stale_evidence`); never changes a vote | none |

The four gates then decide whether the run publishes.

- **Three-valued facts.** Every fact is yes, no or unknown, and scoring works over a points
  interval. A vote is FOR or AGAINST only if every way of filling in the unknowns agrees;
  otherwise it is REVIEW, naming the single facts that would settle it.
- **Models propose, sources decide.** People's seats come from the notice and Companies
  House. CH doesn't say in what capacity someone sits on a board, so a role still unknown is
  asked of the firm's own website and nowhere else: one cited search finds the site (the
  cited page must be on it and name the firm; directories, news and registries never count),
  then the role search runs with the tool limited to that site. The role is asked as of
  the AGM date: the vote is due by then, so a later change doesn't matter. The tool's limit isn't
  airtight (it let SEC filings through), so a role fact citing any page off the site is
  dropped. Site answers only fill facts still unknown. The web is asked freely about
  organisations (listing, parent). A notice quote must be on its cited PDF page, and a web
  fact must quote a page the search returned in that same response. Anything else is
  dropped to unknown.
- **Fail closed.** An outage, an offline cache miss or an error becomes unknown plus a review
  item, never a default value. G2 holds any run with a CH outage.
- **One cache** (`core/cache.py`) for every external call, so a re-run is free and gives the
  same answer.
- **History can't be rewritten.** Decisions, cached responses and every run's results are
  append-only, and database triggers (`core/db.py`) refuse any UPDATE or DELETE on them. A
  decision is undone by appending a revoke. A run's status is set once, and only a staged
  run (gates passed) or an earlier published one (a rollback) can be published. So every
  past answer stays readable: `pennant diff` compares any two runs, and `pennant history`
  follows one director across all of them.

### Swapping a model

Each model-backed task has one interface. Write the new backend, register it, and name it
in `.env`:

| Task | Interface | Register in | Select with |
| --- | --- | --- | --- |
| Read a notice | module with `NAMESPACE`, `notice_key(pdf_bytes)`, `extract_notice(pdf, ctx) -> NoticeExtract` | `extract/backends.py` | `EXTRACT_MODEL` |
| Research a fact | function `(settings, model, prompt, domains) -> payload` (shape in `classify/backends.py`; `domains` limits the search to those sites); raises `ModelUnavailable` on a transient failure | `classify/backends.py` | `LLM_MODEL=<provider>:<model>` |

Every backend gets the same checks. The model name is part of each cache key, so two
models' answers never mix.

### Prompts

Prompts are in `pennant/extract/prompts.md` and `pennant/classify/prompts.md`, one
`## name` section each, sent exactly as written. The extract prompt is part of the
LlamaExtract cache key, so editing it means a paid re-run per notice. Research prompts are
versioned by section name (`org_v1`, `site_v1`, `position_v2`).

## Data model and database

Everything lives in one SQLite file (`PENNANT_DB`, default `pennant.db`), stdlib `sqlite3`, no
ORM. Schema: `core/db.py`; types: `core/models.py` (pydantic) and `core/tri.py` (yes / no /
unknown, Kleene logic, points intervals).

| Table | Holds | Rule |
| --- | --- | --- |
| `cache` | every external answer: (namespace, key) → payload, `fetched_at` (when it last changed), `last_fetched` (when it was last confirmed). Namespaces `llamaextract`, `ch`, `web` | never deleted; only `last_fetched` may change; the newest row wins |
| `decisions` | analyst decisions: `must_link`, `cannot_link`, `set_fact`, `revoke`, each with author and reason | append-only; an undo is a `revoke` |
| `runs` | each run's status, author, config hash and report: inputs seen, stage reports, gates, timings, counters, log path; the published pointer | status set once; only a staged run (or an earlier published one) can be published |
| `run_outputs` | (run, kind, key) → model JSON. Kinds: `meeting`, `roster`, `seat`, `person`, `officer`, `org`, `position`, `recommendation`, `review` | append-only; a later stage writing the same key in the same run supersedes it |
| `model_calls` | every live model call (`calllog.py`) | append-only |

Database triggers refuse any UPDATE or DELETE the rules above don't allow, so no command,
stage or bug can rewrite history. Results are never updated in place: every run writes its
own rows, which is what makes `pennant diff`, `pennant history` and rollback possible.

## Stage interface

- A run is `state/pipeline.run_pipeline(settings, offline, publish, author)`. It creates the
  run, opens its log, runs the stages in order (`STAGES` in `state/pipeline.py`), applies the
  gates and publishes only if asked and every gate passes.
- Each stage is a module with `run(ctx) -> StageReport(stage, ok, counts, errors)`, where
  `ctx = RunContext(conn, run_id, settings, policy, offline)`. A stage reads its inputs from the
  same run's `run_outputs` and writes its own; it never reads another module's internals.
- A stage that returns `ok=False` fails its gate (extract → G1, ch → G2, any other → G4). A
  stage that raises fails the run; the published pointer stays where it was.
- Analyst commands re-run the whole pipeline offline from the cache (seconds) with the analyst
  as author, which skips G3.

## Caching

One function, `core.cache.cached`, sits in front of every external call.

| Source | Key | Good for |
| --- | --- | --- |
| LlamaExtract | PDF hash + schema version + job config (prompt included) | ever: one paid job per notice; a second one is refused unless `--force` |
| Companies House | the request URL; an officer search is one entry per name | until 23:59 local time on the day it was fetched |
| Research model | prompt version + model + inputs | 7 days |

- **Offline** never calls out: it serves whatever is stored, even if expired, and a miss is
  unknown (plus a review item).
- **A refetch with the same answer** adds no row; it only moves `last_fetched`. An officer
  search counts as the same answer when the officers sharing the searched name are the same,
  because Companies House reorders the rest of its fuzzy matches on every request.
- **Failures are never cached**, so the next run retries them.
- **Fixtures:** an offline run on a fresh database first seeds the cache from
  `tests/fixtures/` (only keys it doesn't hold), so it reproduces the recorded live answers.

## Logging and the model call record

- **Per-run log** (`logs.py`): `logs/<database>-<run>.log` next to the database
  (`PENNANT_LOG_DIR` overrides) with each stage's start, end, time and counts, every error with
  its traceback, every live model call with its tokens, and the gate results.
- **Counters** (also `logs.py`), reset per run and saved in the run's report: PDF pages read,
  CH requests, retries and outages, cache hits and refetches per source, LlamaExtract jobs and
  credits, research calls, tokens and searches per question kind, each split into `live`
  (spent this run) and `cached` (what reused answers cost when fetched). `pennant log` reads them.
- **Model call record** (`calllog.py`): every live research call and LlamaExtract job, with
  prompt text, full response, outcome, latency, usage and what validation kept, in the
  append-only `model_calls` table, failed calls included. `pennant calls` summarises it per
  prompt version and model, prints one call, or exports JSONL for analysis.

Instrumentation lives where the calls are made: `core/cache.py`, `ch/client.py`,
`evidence/grounding.py`, `extract/llamaextract.py`, `classify/websearch.py`. Recording never
fails a run.

## Testing

```bash
.venv/bin/pytest -m "not live"                      # offline: about 675 tests, ~20 s, no keys
.venv/bin/pytest -m live                            # calls CH, LlamaExtract and Claude (keys in .env)
.venv/bin/ruff check .
.venv/bin/python eval/gold/score.py --selftest      # the gold scorer grades itself
```

- Offline tests run on recorded responses in `tests/fixtures/`: PDF page text (`pages/`),
  LlamaExtract jobs (`llamaextract/`), CH responses (`ch/`), research answers (`web/`) and
  hand-written seat cases for scoring (`seats/`). Each folder's README says what it holds.
- `tests/conftest.py` gives a `ctx` fixture (in-memory database, offline) and sends run logs
  to a temporary folder. Anything that calls an outside service is marked `live`.
- The hard cases H1–H17 in `CLAUDE.md` are test oracles; `eval/hard_cases.py` reports them on
  any run.

## Extending it

- **A new notice:** put the PDF in `data/notices/` and run live once. It costs one LlamaExtract
  job (about 25 credits a page) and fetches its CH records and research answers. No code names
  a notice, company or person.
- **A new research prompt:** add a `## org_v2` (or `site_v2`, `position_v3`) section to
  `classify/prompts.md` and point `research.PROMPT_VERSION` at it. A new name re-asks every
  question; the old answers stay in the cache and in `model_calls`, so `pennant calls` compares
  the two versions.
- **A new model or provider:** see [Swapping a model](#swapping-a-model). The model name is in
  every cache key, so answers from two models never mix.
- **A new stage:** a module with `run(ctx) -> StageReport`, added to `STAGES` in
  `state/pipeline.py`. It reads and writes `run_outputs` like the others.
- **A policy flag:** `config/policy.yaml` and the `Policy` model in `core/config.py`; scoring is
  in `policy/score.py`. The gold set models the same flags (`eval/gold/build_gold.py`).

## Recording fixtures (live, once)

| What | Command |
| --- | --- |
| LlamaExtract (refuses a second paid run per notice unless `--force`) | `.venv/bin/python -m pennant.extract.llamaextract record` |
| Companies House | `.venv/bin/python scripts/record_ch_fixtures.py` |
| Web research | after a live run, `pennant.classify.websearch.export_fixtures(conn, directory)` writes every cached answer; copy in the files `tests/fixtures/web/` doesn't have yet |

## Layout

| Path | What |
| --- | --- |
| `docs/` | this guide, the [user guide](user-guide.md) and the [pipeline diagrams](pipeline.html) |
| `config/policy.yaml` | threshold (4 points) and the policy flags (AIM, debt-only, chair-designate, group dedupe) |
| `pennant/core` | settings and policy, models, three-valued logic, database, cache |
| `pennant/extract`, `evidence` | extract backends and prompts, meeting and quote checks against the PDF, citations, evidence freshness |
| `pennant/ch`, `dedup` | CH client; anchor, date-of-birth expansion and clustering into people |
| `pennant/classify`, `policy` | role rules, research backends and prompts; reconciliation, scoring and the score stage |
| `pennant/state` | pipeline and gates, decisions, run inputs, diff, history |
| `pennant/cli.py`, `cli_view.py`, `cli_show.py`, `cli_changes.py`, `cli_log.py`, `cli_style.py` | the `pennant` commands; reading a run and printing votes and review items; `show`; previewing and confirming changes, `history`; `log` and `calls`; shared tables and colours (`rich`) |
| `pennant/logs.py`, `calllog.py` | per-run log and counters; the model call record |
| `pennant/prompts.py` | loads a package's `prompts.md` |
| `eval/` | export, metrics, hard cases; `eval/gold/` is the evidence-built answer key |
| `scripts/` | `nightly.sh` and `install_nightly.sh` (the launchd job); `record_ch_fixtures.py` |
| `tests/fixtures/` | recorded LlamaExtract, page text, CH and web responses; hand-written seat cases |
| `logs/` | per-run log files (git-ignored) |

## Further reading

- [`CLAUDE.md`](../CLAUDE.md): the build spec, step cards and hard-case catalog.
- [`eval/gold/README.md`](../eval/gold/README.md): how the gold set was built from cited evidence.
- `tests/fixtures/*/README.md`: what each recording holds and how it was made.
- Module docstrings: each states its design choice.
