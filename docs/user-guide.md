# User guide

For analysts running `pennant`: setting it up, running it, reading and correcting its votes,
and checking what a run did. How it works inside is in the [technical guide](technical-guide.md);
the [pipeline diagrams](pipeline.html) show every stage.

- [Setup](#setup)
- [Running](#running) (logs, model calls, the nightly run, gates)
- [Commands](#commands)
- [Evaluation](#evaluation)
- [Demo](#demo)
- [Known gaps](#known-gaps)

## Setup

Needs Python 3.12. From a folder holding the code and the PDFs in `data/notices/`:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/pytest -m "not live"
```

`.env` settings:

| Key | Used for |
| --- | --- |
| `CH_API_KEY` | Companies House REST API (free key) |
| `LLAMA_CLOUD_API_KEY` | LlamaExtract, `agentic` tier: about 25 credits a page, once per notice |
| `ANTHROPIC_API_KEY` | Claude with the web search tool |
| `LLM_MODEL` | research model as `[provider:]model`; default `claude-sonnet-5-5` (provider `anthropic`) |
| `EXTRACT_MODEL` | notice reader; default `llamaextract` |
| `PENNANT_DB` | SQLite file, default `pennant.db` |
| `PENNANT_OFFLINE` | `1` = never call an API; use cached and recorded answers only |

Web search must be enabled for your organisation in the Anthropic Console. The keys
are only needed for live runs; offline runs and tests use none.

## Running

```bash
.venv/bin/pennant run --offline --publish   # from recorded fixtures, no keys, ~2 s
.venv/bin/pennant run --publish             # live: LlamaExtract, CH and Claude
```

- **Offline** seeds the cache from the recordings in `tests/fixtures/` (LlamaExtract,
  CH, web) on first use, so a fresh database gives the same answers as the live run
  that recorded them. A notice with no recording can only run live.
- **Live** goes through the same cache: a second run the same day is free and identical.
  Each notice is sent to LlamaExtract once; Companies House answers are good until 23:59
  local time on the day they were fetched; web answers are kept for 7 days. A refetch that
  returns the same answer adds no row: it only moves that row's `last_fetched` date, while
  `fetched_at` keeps when the answer last changed. An officer search is one answer per name:
  the client reads pages until it is past the officers sharing the surname and first
  forename, and counts a refetch as the same answer when those officers are the same, since
  CH reorders the rest of its fuzzy matches on every request.
- Every run writes a new snapshot. Without `--publish` it ends `staged`; read
  commands keep showing the published run. `pennant run` exits 1 if the run is
  `held` or `failed`.

### Logs

Every run writes its own log, `logs/<database>-<run>.log` next to the database
(`logs/pennant-run-1.log`; `PENNANT_LOG_DIR` overrides the folder): each stage's start, end,
time and counts, every error with its traceback, each live LlamaExtract job and research call
with its tokens, Companies House retries and outages, and the gate results. The run's report
also keeps the counters, so `pennant log` works for any run:

This is the live run from an empty database on 27 Oct: 46 minutes, nearly all of it web
research, and the only paid work (LlamaExtract was replayed from its recordings):

```text
$ pennant log run:1
run:1: published  by system  started 2026-10-27T18:29:05+00:00  finished 2026-10-27T19:15:19+00:00

Stages
Stage           Result     Time  Counts
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
stage extract   ok         1.1s  meeting=5, review=0, roster=50, seat=230, ungrounded=0
stage ch        ok       110.7s  appointments_unavailable=0, issuers=5, issuers_ok=5, officers=49, recently_resigned=5
stage dedup     ok       159.0s  links=152, mentions=50, people=47, reviews=8, skipped_ch_unavailable=0
stage classify  ok      2502.8s  listed_by_rule=117, org_research=81, orgs=285, position_research=34, position_targets=38, seats=230
stage score     ok         0.1s  AGAINST=7, FOR=35, REVIEW=8, recommendations=50, review_items=31, seats=259
stage evidence  ok         0.0s  dated_orgs=117, dated_persons=46, dated_seats=144, stale_citations=0

Work
Source           Counts
─────────────────────────────────────────────────────────────────────────────────────────────────────────
cache            ch.fetched=395, ch.hit=114, ch.new=395, llamaextract.hit=5, web.fetched=146, web.new=145
Companies House  requests=452
LlamaExtract     credits_cached=1775, jobs_cached=5
PDF              pages_read=130

AI usage (research model)
Question         Calls  Input tokens  Output tokens  Searches
─────────────────────────────────────────────────────────────
org (live)          81     5,478,869        221,058       340
position (live)     34     1,485,174         44,177        78
site (live)         31       827,425         18,188        57
total (live)       146     7,791,468        283,423       475
live = spent by this run; cached = what the reused answers
cost when they were fetched

  log file: logs/pennant-run-1.log
```

- **Counted:** PDF pages read; Companies House requests, retries and outages; cache hits,
  fetches, unchanged and changed refetches per source; LlamaExtract jobs and credits (it
  reports credits, not tokens); research calls, input and output tokens and web searches, per
  question kind (`org`, `site`, `position`).
- **Live vs cached:** `live` is what this run spent; `cached` is what the answers it reused
  cost when they were first fetched, so a fully cached run still shows what its inputs cost.
- `pennant run` prints one line of this ("Work this run: …") and the log's path;
  `pennant log --full` also prints the log file.

### Model calls, for tuning prompts and models

Every live call to the research model or LlamaExtract is also appended to the `model_calls`
table (`pennant/calllog.py`): the run, the question kind and inputs, the prompt version and
the exact prompt text, the model, the full response (search queries, results, answer), the
outcome (`ok`, `rejected` for an unusable answer, `unavailable`, `error`), latency, tokens,
searches or credits, and which facts validation kept or dropped and why. Failed calls are
recorded too, though they are never cached. The table is append-only.

```bash
.venv/bin/pennant calls                              # per kind, prompt version and model
.venv/bin/pennant calls --show 200                   # one call: prompt, answer, validation
.venv/bin/pennant calls --kind position --export position_calls.jsonl   # for analysis
.venv/bin/pennant calls --backfill                   # once: answers cached before the record
```

```text
$ pennant calls
Model calls
Kind      Prompt       Model              Calls  ok  Rejected  Unavailable  Error  Secs  In tokens  Out tokens  Searches  Kept  Dropped  Credits
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
org       org_v1       claude-sonnet-5-5     82  81         1            0      0  22.8     68,088       2,730       4.2   660        3        0
position  position_v2  claude-sonnet-5-5     34  34         0            0      0  12.5     43,682       1,299       2.3    82        0        0
site      site_v1      claude-sonnet-5-5     31  31         0            0      0   7.7     26,691         587       1.8    28        0        0
secs, tokens and searches are per call; kept / dropped are facts validation kept or dropped; credits (LlamaExtract) are the total
pennant calls --show <id> prints one call; ids 1–147
```

`--backfill` copies in answers cached before the record existed, with their responses,
tokens and validation but no prompt text.

### Nightly run

A launchd job runs the pipeline live every night at 02:00 and publishes if every gate
passes. It is installed on this Mac:

```bash
scripts/install_nightly.sh            # install (or reinstall), every night at 02:00
scripts/install_nightly.sh 5 30       # at 05:30 instead
scripts/install_nightly.sh --remove   # uninstall
```

- **What it picks up:** any new or changed PDF in `data/notices/` (one LlamaExtract job per
  new notice), every Companies House answer fetched again (they went stale at 23:59), and web
  answers older than 7 days.
- **Idempotent:** a night with no new data publishes an identical snapshot (`Diff: no
  changes`), and identical CH answers add no cache rows. A vote that flips with no changed
  input holds the run (G3), and a CH outage holds it (G2); either way the last published
  votes stand.
- **Logs:** `~/Library/Logs/pennant/nightly-<date>.log`. A held or failed run also raises a
  macOS notification. `launchctl kickstart gui/$UID/com.pennant.nightly` runs it now.
- launchd, not cron: a run missed while the Mac sleeps happens when it wakes.
- **First night (27 Oct, before the database was reset):** published with every gate passing and no vote change;
  352 of 432 CH answers were identical. The other 80 were officer searches that CH had only
  reordered, which is what led to comparing searches by namesakes; a full refetch of all 45
  searches since then added no rows.

### What stops one bad run from corrupting every record

The nightly job runs unattended, so every safeguard is built into the data, not left to
someone watching:

- **Nothing is overwritten.** A run writes its own snapshot of every meeting, seat, person
  and vote (`run_outputs`); it never edits an earlier run, a cached answer or a decision.
  Database triggers refuse it, so no bug can. A bad run is just one more row set beside the
  good ones.
- **Votes come from one pointer.** `votes`, `show` and `review` read only the published run.
  A run moves the pointer only if every gate passes; a held or failed run changes nothing
  anyone reads.
- **Gates catch the usual failures** (see [Gates](#gates)): a Companies House outage (G2), a
  FOR↔AGAINST flip with no changed input behind it (G3), an unverifiable quote behind an
  AGAINST (G1), a missing vote or an ignored decision (G4).
- **Failing closed.** An error or a missing answer becomes `unknown` plus a review item,
  never "no". An outage can turn a vote into REVIEW, but it can't silently turn it into FOR.
- **Rollback**, for whatever the gates miss. `pennant runs` lists recent runs and their vote
  counts. `pennant rollback run:N` publishes an earlier run's votes again (only a run that
  passed its gates can be published), and `pennant diff` shows what changed back. A rollback
  stays in effect: later runs, nightly or after an analyst decision, are staged but not
  published, because they read the same cache that may hold what went wrong. Check a staged
  run with `pennant diff`, then `pennant publish run:N` to resume. Demo step 6 walks through it.
- **Decisions are not rolled back.** Rolling back moves which votes are shown; analyst
  decisions stay recorded and replay on every later run. Undo a bad decision with
  `pennant undo`.

### Gates

A run is published only if every gate passes. Otherwise it is `held` (or `failed` on
an exception) and the last published votes stand.

| Gate | Holds the run when |
| --- | --- |
| G1 extraction | resolution numbers aren't contiguous, or an ungrounded quote sits behind an AGAINST |
| G2 Companies House | any CH lookup was unavailable (issuer officer list or one director's appointments) |
| G3 churn | a FOR↔AGAINST flip versus the published run has no changed input behind it (system runs only) |
| G4 completeness | a roster entry has no recommendation, or an analyst decision isn't honoured |

```text
$ pennant run --offline --publish
run:1: published
  G1 pass   extraction
  G2 pass   Companies House
  G3 pass   churn
  G4 pass   completeness
  Work this run: 130 PDF pages read; 0 CH requests; 0 LlamaExtract jobs (0 credits); 0 research calls (0 in / 0 out tokens, 0 searches)
  Log: logs/pennant-run-1.log  (details: pennant log run:1)
```

A held run lists each failing gate's problems, and `pennant run` exits 1. After a rollback, a run that passes its gates ends `staged`, not published, until you publish it (demo step 6).

## Commands

Read commands show the **published** run. Add `--run run:N` to read another; if
nothing is published yet they show the latest run, labelled `NOT PUBLISHED`.
Write commands first show exactly what they will record (the fact, its current and new value,
and each affected director with their vote now) and ask `Record it? [y/N]`, defaulting to no;
`--yes` (`-y`) skips the question for scripts. Then they append a decision, re-run offline from
cache (G3 skipped), publish if the gates pass, and print the diff. Decisions are replayed on
every later run until undone.

| Command | What it does |
| --- | --- |
| `pennant run [--offline] [--publish]` | run the pipeline; print the gate report and the diff against the published run |
| `pennant votes [--meeting M]` | per director: role, points `lo–hi`, rule fired, vote, coverage. `M` is a meeting key, company number or part of the name |
| `pennant show <name> [--evidence]` | the vote first (points, rules, the facts that would flip it), then mentions, CH records, a ✓ / ✗ / ? table of every seat's facts, and each seat's citations; organisations' listing sources are counted unless stale or `--evidence` lists them |
| `pennant calls [--run] [--kind] [--show id] [--export file] [--backfill]` | recorded live model calls: per prompt version and model (calls by outcome, latency, tokens, facts kept and dropped), one call in full, or all as JSONL |
| `pennant log [run] [--full]` | what a run did: stage times and counts, errors, pages read, CH requests, cache use, AI tokens and LlamaExtract credits (latest run by default) |
| `pennant history <name> [--all]` | the director's vote at each run where it changed (or every run), why it changed, the changes to their seats and organisations, and every decision about them |
| `pennant review` | open review items, each with a suggested command |
| `pennant decisions [--active]` | every analyst decision in one place, oldest first: what was decided (with names), who, why, and whether it is active, undone or replaced by a newer one |
| `pennant merge <a> <b> -r "why"` | these are one person (must-link); `a` and `b` are names, person or mention ids, or a CH officer id, as review items suggest |
| `pennant split <person> --move <mention or meeting key> -r "why"` | these mentions are someone else (cannot-link). The newest decision wins: a split that contradicts an earlier merge (or a merge that contradicts an earlier split) names the decision it replaces, asks, and undoes it first |
| `pennant set-fact <seat_id \| org_key \| mention_id> <field> <value> -r "why"` | analyst override of one fact |
| `pennant undo <dec:N>` | revoke a decision |
| `pennant diff [run_a] [run_b]` | compare two runs (default: published vs latest) |
| `pennant runs [-n 20]` | recent runs: status, who ran it, vote counts, which one is published |
| `pennant rollback <run:N>` | publish an earlier run's votes again; later runs are staged, not published, until you publish one |
| `pennant publish <run:N>` | publish a staged run, e.g. to end a rollback once its diff looks right |
| `pennant eval` | grade the published run against `eval/gold/`, write `eval/report.md` |

**Output.** Tables fit the terminal width; votes are coloured (FOR green, AGAINST red, REVIEW
yellow) and facts show as ✓ yes, ✗ no, ? unknown. Piped or redirected output (`> file`) is plain
text, 160 columns wide.

Names are matched case- and accent-insensitively (`pennant show "andre lacroix"`). An
ambiguous name lists the candidates:

```text
$ pennant show murphy
'murphy' is ambiguous; use one of these ids:
  p:nm:00445790:r10  Dr Gerry Murphy  (Tesco r10)
  p:nm:00445790:r11  Ken Murphy  (Tesco r11)
```

`set-fact` fields: on a seat `board_seat`, `chair`, `executive`, `current`, `listed`,
`identity`, `role_type`; on an org `listed`, `equity_listing`, `market`,
`parent_name`, `org_type`; on a roster entry `role_type_at_issuer`. Tri values are
`yes`, `no` or `unknown`. Under `Unknown facts that decide this vote`, `show` asks each
fact as a question and prints the exact `set-fact` command that settles it.

Every `show`, `votes` and `review` screen ends its sections with a key to the codes it
uses (vote, points, rules, coverage, seat status, ✓ / ✗ / ?, review kinds), so the output
reads on its own. In `show`, each seat's sources are a table (from, says, checked), with
the links numbered underneath so a URL stays on one line. Under the seats, `Other Companies
House appointments (not counted)` lists every directorship on the person's CH records that
can't change the vote, with why: ended before the AGM, a UK private or guarantee company
(which can't list), or found unlisted. So no other appointments shown means there are none.

`review` output:

```text
$ pennant review
44 review items  [run:2]   6 kinds: not_in_office, possible_same_person, site_not_found, stale, undisclosed, web_unsourced
  not_in_office  (1)  matched to a Companies House record, but not a director of this company on the AGM date
    [not_in_office] nm:00445790:r7: Thierry Garnier matches GARNIER, Thierry Dominique Gerard (WnytyEoBEEno7UhOqQ-tsXfC5zk), a director of 00445790 2021-04-30 to 2026-05-06 but not on the AGM date 2026-06-18; the notice's seat is checked against those dates
        -> pennant show "Thierry Garnier"
  possible_same_person  (6)  two Companies House records match about equally; pick one
    [possible_same_person] A4czjaYDU4d1WgoezgBICX4smQQ: Andrew Peter MACHIN (haHeusxScAvvOmqMBPp87ogYjSI) shares Stuart Barry MACHIN's date of birth 10/1970, but the names only match at variant
  …
    [undisclosed] seat:-8n4-Ctsi6TOgxR0XirDsMIVgLc:08629715: p:nm:00445790:r12: CH shows director of TESCO CORPORATE TREASURY SERVICES PLC on 2026-06-18; not in the notice
        -> pennant set-fact seat:-8n4-Ctsi6TOgxR0XirDsMIVgLc:08629715 board_seat <yes|no> --reason "..."
  …
```

Kinds, by where they come from:

- **Notice:** `ungrounded_quote`, `ungrounded_role` (dropped as unverifiable), `extract_error`, `extract_warning`.
- **CH and identity:** `undisclosed` (a CH directorship the notice left out), `stale`,
  `contradiction`, `ch_unavailable`, `unanchored`, `possible_same_person`, `no_dob`, `link_refused`.
- **Web research:** `web_research_failed`, `web_unsourced`.
- **Decisions and scoring:** `decision_conflict`, `decision_stale`, `score_error`.

## Evaluation

```bash
.venv/bin/pennant eval
```

Exports the published run to `eval/out/` (`roster.csv`, `votes.csv`, `seats.csv`,
git-ignored), grades it with the gold scorer in `eval/gold/`, and writes
[`eval/report.md`](../eval/report.md): wrong AGAINST (the headline, target 0), wrong FOR,
votes split into correct, deferred (REVIEW where gold decides) and wrong, which add up to the scored
votes; pairwise identity precision and recall, countable seats,
the result under each `aim_counts_as_listed` value, and a line per hard case H1–H17.
A hard case or a missed seat that differs from gold only because the run left a fact unknown is
reported as DEFERRED (or "not wrong: … unknown"), with the fact that would settle it, rather than as
a failure: REVIEW is the system saying it isn't sure, not a wrong answer.
A meeting the gold set doesn't cover is listed, not graded. The gold set, its
sources and the scorer are described in [`eval/gold/README.md`](../eval/gold/README.md).

### Results (27 Oct 2026)

Graded against the gold set as rebuilt from cited evidence ([`eval/gold/README.md`](../eval/gold/README.md)):
41 FOR, 7 AGAINST, 1 REVIEW, and Garnier's withdrawn resolution, which isn't scored. Policy as in
`config/policy.yaml`: AIM counts as listed, bond-only listings count, a chair-designate isn't chair.

| Run | Wrong AGAINST | Wrong FOR | Accuracy | REVIEW | Identity (pairwise P/R) | Countable seats (P/R) |
| --- | --- | --- | --- | --- | --- | --- |
| Live, published (`run:7` in `pennant.db`) | 0 | 0 | 90% | 10% | 100% / 100% | 100% / 98% |
| Offline, fresh database | 0 | 0 | 94% | 6% | 100% / 100% | 99% / 98% |

The database was reset on 27 Oct and rebuilt live from the five PDFs. The first run from empty
(`run:1`) scored 86%: one research answer, IG Design Group's listing, came back without the JSON
the validator needs, and Tom Delay wasn't yet matched. `run:2` re-asked the one question and added
the nickname list, and both were settled. `run:7` added the short-name officer search, which
found Roger Burnley's Pets at Home directorship on a CH record filed as plain "Roger BURNLEY".

50 roster mentions become 47 people, and all 47 anchor to Companies House: Tom Delay through the
nickname list ("Tom" is a listed nickname of "Thomas"). Every disagreement with gold is a REVIEW,
never a wrong vote. Companies House records every seat as plain "director", so the capacity
(chair or not, executive or not) comes from the notice or, failing that, the firm's own website: the
M&S roles (its notice has no biographies), and the CH-only seats of Ashton, Cooke, Cosslett, Ewing and
Nebhrajani, are settled that way. Banks and a group's finance company often list only bonds (Lloyds
Bank, Bank of Scotland, HSBC UK Bank, Tesco Corporate Treasury Services); with bond-only listings counting
they are listed seats, but each shares a group with another seat of the same director, so it adds no
points. The REVIEWs left: Kawal Preet (FedEx's site doesn't say whether she was on its board on the AGM
date), Graham Allan (Bata's listing unsourced), Laura Crespi (no own website found for Youngever) and,
live only, Dame Carolyn Fairbairn (whether her HSBC UK Bank seat is executive) and Graham Cooke
(Qubit's board seat, role and listing). Each REVIEW names the fact that would settle it, ready for
`pennant set-fact`.

## Demo

Offline, on a fresh database, so no keys are needed and run and decision ids match:

```bash
export PENNANT_OFFLINE=1 PENNANT_DB=/tmp/pennant-demo.db
rm -f $PENNANT_DB
.venv/bin/pennant run --offline --publish
```

**1. Tesco votes.** 11 directors. Chris Kennedy is AGAINST under the executive rule (ITV CFO and
executive director, plus this seat). Stewart Gilliland is AGAINST under the executive rule too: he is
interim executive chair of IG Design Group, which is on AIM, and the policy counts AIM as listed
(with `aim_counts_as_listed: "unknown"` he would be REVIEW). Caroline Silver is FOR at 4 points:
Moelis's own governance page doesn't list her among its directors. Thierry Garnier is REVIEW: CH shows
him leaving the Tesco board on 6 May 2026, before the AGM (Tesco withdrew his re-election), so his
Tesco seat is stale.

```text
$ pennant votes --meeting tesco
Tesco PLC — AGM 2026-06-18  [run:1]
Res  Director                Role                     Points  Rule        Vote     Coverage
─────────────────────────────────────────────────────────────────────────────────────────────
  4  Melissa Bethell         NED                           4  -           FOR      structured
  5  Bertrand Bodson         NED                           1  -           FOR      structured
  6  Dame Carolyn Fairbairn  NED                           3  -           FOR      structured
  7  Thierry Garnier         NED                         1–2  exec rule?  REVIEW   structured
  8  Stewart Gilliland       NED                           3  exec rule   AGAINST  structured
  9  Chris Kennedy           NED                           2  exec rule   AGAINST  structured
 10  Dr Gerry Murphy         Chair                         4  -           FOR      structured
 11  Ken Murphy              Officer (board unknown)       1  -           FOR      structured
 12  Imran Nawaz             Officer (board unknown)       1  -           FOR      structured
 13  Caroline Silver         NED                           4  -           FOR      structured
 14  Karen Whitworth         NED                           3  -           FOR      structured
  8 FOR · 2 AGAINST · 1 REVIEW    pennant show <name> explains any vote
  …key to the codes…
```

**2. Caroline Silver.** `show` starts with the vote and why, then lists each seat with its notice
citation, the CH appointment that confirms it, the cited web sources behind each organisation's
listing, and, for a role the notice leaves open, the firm's own page:

```text
$ pennant show "Caroline Silver"
Caroline Silver  (p:nm:00445790:r13)  [run:1]

Vote at Tesco: FOR   4 points
  FOR because neither rule applies
  Points rule doesn't apply: 4 points against a limit of 4 (a non-executive here)
  Executive rule doesn't apply: against an executive at a listed company who also holds a counted board seat in another group
  Seats that score points or are executive:
    • Tesco PLC (Independent Non-executive Director): 1 pt [confirmed]
    • Barratt Redrow plc (Chair): 2 pt [confirmed]
    • Intercontinental Exchange, Inc. (Non-executive director): 1 pt [ch_invisible]
  Key: FOR: neither rule applies · AGAINST: a rule applies · REVIEW: an unknown fact decides whether a rule applies
  Points: 1 per counted board seat, 2 if it is a chair; a range like 0–1 means a fact about that seat is unknown; seats in one corporate group count once
  [status] after a seat: see the Seats key below
…

Seats (every role found, and the facts the vote used)
Organisation                               Role                                Status        Board  Chair  Exec  Current  Listed  ID
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Barratt Redrow plc                         Chair                               confirmed     ✓      ✓      ✗     ✓        ✓       ✓
board of trustees of V&A Foundation        Other                               ch_invisible  ✗      ✗      ✗     ✓        ?       ✓
ICE Clear Europe                           Chair                               confirmed     ✓      ✓      ✗     ✓        ✗       ✓
Intercontinental Exchange, Inc.            Non-executive director              ch_invisible  ✓      ✗      ✗     ✓        ✓       ✓
International Advisory Board of Adobe Inc  Other                               ch_invisible  ✗      ✗      ✗     ✓        ?       ✓
Moelis & Company                           Other                               ch_invisible  ✗      ✗      ?     ✓        ✓       ✓
National Film and Television School        Chair of audit committee            ch_invisible  ✗      ✗      ✗     ✓        ?       ✓
Tesco PLC                                  Independent Non-executive Director  confirmed     ✓      ✗      ✗     ✓        ✓       ✓
…
  Status ch_invisible: not on Companies House (not a UK company); facts come from the notice and web research

Sources
  Where each fact comes from: the notice (page and exact words), the Companies House register, and web pages behind each organisation's listed status. Last checked: the last day the source confirmed it (a notice quote is found again in the PDF on every run). STALE: last checked before the AGM, so it may be out of date

  Barratt Redrow plc   seat:00445790:r13:1
    From                 Says                                                  Last checked
    ───────────────────────────────────────────────────────────────────────────────────────
    Tesco notice p.10    "Chair: Barratt Redrow plc and ICE Clear Europe"      2026-10-27
    Companies House [1]  "BARRATT REDROW PLC: director, appointed 2023-06-01"  2026-10-26
    [1] https://find-and-update.company-information.service.gov.uk/officers/sYzrAgP05-XBMuExY4hjznwnRxc/appointments
    Listed: yes on London Stock Exchange (BTRW) (10 web sources; `pennant show --evidence` lists them)
…
  Moelis & Company   seat:00445790:r13:7
    From               Says                                                                                                       Last checked
    ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    Tesco notice p.10  "Other: International Advisory Board of Adobe Inc, board of trustees of V&A Foundation, Moelis & Company"  2026-10-27
    Web [1]            "{"name":"Kenneth L. Shropshire","jobTitle":"Director"}"                                                   2026-10-27
    [1] Moelis & Company - Governance - Executive Officers & Directors https://investors.moelis.com/governance/executive-officer-directors/default.aspx
    Listed: yes on New York Stock Exchange (MC) (6 web sources; `pennant show --evidence` lists them)
…
```

ICE Clear Europe scores nothing: CH says it is a private company, and research names
Intercontinental Exchange as its parent. Moelis's directors page lists its board (one entry is
quoted) without her, so her Moelis role is not a board seat.

**3. Settle a REVIEW.** Kawal Preet (Intertek) is FedEx's EVP; FedEx's own site doesn't say whether
she sat on its board on the AGM date, so her vote waits on that one fact. The command shows what it
will record and asks first; the diff names the decision:

```text
$ pennant set-fact seat:04267576:r13:5 board_seat no --reason "FedEx's board page doesn't list her: an EVP, not a director"
Set seat seat:04267576:r13:5 (FedEx — Executive Vice President, Planning, Engineering and Transformation) board_seat: unknown → no
  affects Kawal Preet (Intertek r13: REVIEW)
This is appended to the decision log: every later run replays it until you `pennant undo` it. The pipeline re-runs now and publishes only if every gate passes.
Record it? [y/N]: y
Recorded dec:1: set_fact {'field': 'board_seat', 'kind': 'seat', 'target': 'seat:04267576:r13:5', 'value': 'no'}
run:2: published
…
Diff: run:1 → run:2
dec:1: set_fact {'field': 'board_seat', 'kind': 'seat', 'target': 'seat:04267576:r13:5', 'value': 'no'} by analyst: FedEx's board page doesn't list her: an EVP, not a director
seat seat:04267576:r13:5 (FedEx): board_seat unknown → no (dec:1)
Kawal Preet @ intertek: vote REVIEW → FOR; points 1–2 → 1–1; exec_rule unknown → no (dec:1)
```

**4. Undo it, and see her history.** Later commands pass `-y` to skip the question.

```text
$ pennant undo dec:1 -y
…
Recorded dec:2: revoke dec:1
…
Kawal Preet @ intertek: vote FOR → REVIEW; points 1–1 → 1–2; exec_rule no → unknown (dec:2)

$ pennant history "Kawal Preet"
Kawal Preet  (p:nm:04267576:r13)

Votes at Intertek r13 (runs where it changed)
Run      Status     Finished          Vote    Points  Rule        Why
───────────────────────────────────────────────────────────────────────────
run:1    published  2026-10-27 19:19  REVIEW  1–2     exec rule?  first run
run:2    published  2026-10-27 19:19  FOR     1–1     -           dec:1
run:3 *  published  2026-10-27 19:19  REVIEW  1–2     exec rule?  dec:2
* the published run (run:3)

Changes to their records (person, seats, organisations)
  run:1 → run:2  seat seat:04267576:r13:5 (FedEx): board_seat unknown → no  (dec:1)
  run:2 → run:3  seat seat:04267576:r13:5 (FedEx): board_seat no → unknown  (dec:2)

Decisions about this director
Id     Made              By       Decision                                      Reason
─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
dec:1  2026-10-27 19:19  analyst  set seat seat:04267576:r13:5 board_seat = no  FedEx's board page doesn't list her: an EVP, not a director  revoked by dec:2
dec:2  2026-10-27 19:19  analyst  undo dec:1                                    undo
```

**5. Chris Kennedy, one person across two notices.** Both mentions anchor to the same CH officer
(Christopher John KENNEDY, born 1/1964, director of Tesco and ITV), so `show` gives one person with
an AGAINST at each meeting. Split him, then undo:

```text
$ pennant split "Chris Kennedy" --move itv --reason "test: treat the ITV mention as someone else" -y
Split Chris Kennedy (Tesco r9: AGAINST, ITV r12: AGAINST): ITV r12 becomes someone else; Tesco r9 stays
Recorded dec:3: cannot_link {'a': ['nm:04967001:r12'], 'b': ['nm:00445790:r9']}
run:4: published
…
p:nm:00445790:r9 split into p:nm:00445790:r9, p:nm:04967001:r12 (dec:3)
  Chris Kennedy @ itv: vote AGAINST → REVIEW; points 2–2 → 1–3; exec_rule yes → unknown (dec:3)
seat seat:04967001:r12:0 (ITV plc): status confirmed → ch_unavailable (dec:3)

$ pennant undo dec:3 -y
Recorded dec:4: revoke dec:3
…
p:nm:00445790:r9 merged with p:nm:04967001:r12 (dec:4)
  Chris Kennedy @ itv: vote REVIEW → AGAINST; points 1–3 → 2–2; exec_rule unknown → yes (dec:4)
seat seat:04967001:r12:0 (ITV plc): status ch_unavailable → confirmed (dec:4)
```

**6. Roll back a bad run.** Say a run publishes something wrong. Here an analyst merges Ken and Gerry
Murphy; the command warns that both stand at Tesco, and once confirmed it is honoured, so both
become AGAINST:

```text
$ pennant merge "Ken Murphy" "Gerry Murphy" --reason "same person" -y
Merge into one person: Ken Murphy (Tesco r11: FOR) and Dr Gerry Murphy (Tesco r10: FOR)
Warning: both stand for election at tesco. A notice names each director once, so these are normally two people.
…
run:6: published
…
  Dr Gerry Murphy @ tesco: now shown as Ken Murphy (vote FOR → AGAINST; exec_rule no → yes; role_at_meeting chair → officer_title) (dec:5)
  Ken Murphy @ tesco: vote FOR → AGAINST; points 1–1 → 4–4; exec_rule no → yes (dec:5)

$ pennant runs -n 3
Runs
Run    Status     By       Started           Votes
───────────────────────────────────────────────────────────────────────────────────────
run:6  published  analyst  2026-10-27 19:20  36 FOR · 9 AGAINST · 4 REVIEW  ◀ published
run:5  published  analyst  2026-10-27 19:20  38 FOR · 8 AGAINST · 4 REVIEW
run:4  published  analyst  2026-10-27 19:20  38 FOR · 7 AGAINST · 5 REVIEW

$ pennant rollback run:5 -y
Publish run:5 (published, by analyst, started 2026-10-27T19:20) in place of run:6
  votes now:   36 FOR · 9 AGAINST · 4 REVIEW
  votes after: 38 FOR · 8 AGAINST · 4 REVIEW
  This is a rollback: until you publish a run newer than run:6, new runs (nightly or after a decision) are staged, not published.
Published run:5.
…
  Dr Gerry Murphy @ tesco: no longer shown as Ken Murphy (vote AGAINST → FOR; exec_rule yes → no; role_at_meeting officer_title → chair) (dec:5)
  Ken Murphy @ tesco: vote AGAINST → FOR; points 4–4 → 1–1; exec_rule yes → no (dec:5)
```

The votes are back, but the decision still stands, so the next run would bring the AGAINSTs back.
That is why a rollback holds later runs, nightly or after a decision: they are staged, not
published.

```text
$ pennant run --offline --publish
run:7: staged
…
  Not published; votes still come from run:5.
…
  Not published: rolled back from run:6 to run:5; runs are staged, not published, until one is
  published with `pennant publish`. Check it with `pennant diff`, then `pennant publish run:7`.
…
  Ken Murphy @ tesco: vote FOR → AGAINST; points 1–1 → 4–4; exec_rule no → yes (dec:5)
```

Undo the decision, check the new run against the rolled-back votes, and publish it to end the
rollback:

```text
$ pennant undo dec:5 -y
…
Recorded dec:6: revoke dec:5
run:8: staged
…
  Not published; votes still come from run:5.

$ pennant diff
Diff: no changes from run:5 to run:8

$ pennant publish run:8 -y
Publish run:8 (staged, by analyst, started 2026-10-27T19:20) in place of run:5
  votes now:   38 FOR · 8 AGAINST · 4 REVIEW
  votes after: 38 FOR · 8 AGAINST · 4 REVIEW
  This ends the rollback: later runs publish again when their gates pass.
Published run:8.
```

**7. Nightly safety (H16).** With CH unavailable for one issuer, the run is held and the published
votes don't change:

```bash
.venv/bin/pytest -k "h16 or rollback"     # 13 passed
```

**8. Evaluation.** `pennant eval` writes `eval/report.md` (see [Evaluation](#evaluation)).

## Known gaps

- **Roles neither the notice nor the firm's own site states stay REVIEW** until an analyst sets them
  (Kawal Preet at FedEx). Re-asking can tip such a case either way: the role question re-runs
  weekly, and a firm whose site shows only part of its board list (Moelis, FedEx) may get "unknown"
  one week and "no" the next. Each answer explains itself in the cached `note`. A firm with no website of its own found (Youngever, HBOS, easyJet's aircraft
  company) gets a `site_not_found` review item. A site shows its board as it is today, not on the
  AGM date; the prompt asks for start and end dates, and a role that began after the AGM or ended
  before it is not counted as current.
- **A prompt or code change isn't an input change to `pennant diff`.** Switching a prompt version
  picks other cached answers without adding cache rows, so the flips it causes are tagged "no recorded
  input change". G3 then holds any such FOR↔AGAINST flip, which is the safe direction.
- **Group parents in live runs.** Web research never runs for a UK private company (CH settles that it
  isn't listed), so in a fresh live run ICE Clear Europe and Hafren Dyfrdwy have no parent, and hard case
  H8 fails. No vote depends on it, because a private subsidiary scores nothing. Offline, the recorded
  answers supply the parents.
- **Withdrawn resolutions** aren't detected: the notice still lists Garnier, so he is REVIEW rather than
  absent. He anchors to his former Tesco record (no one in office matches), so his Tesco seat is stale
  and never counts toward an AGAINST.
- **Unanchored directors** are scored without CH until an analyst links them, e.g.
  `pennant merge nm:02366619:r9 3foiCz6Hg_2s4JTbIRFd01JBwG4 -r "..."`. Since the nickname list
  (`pennant/dedup/data/`), all 50 example mentions anchor; a nickname the list doesn't have still
  falls below the name-match bar.
- **Seats in neither the notice nor Companies House** are not found. The M&S notice has no
  biographies, so Fiona Dawson's chair of Kerry Group (an Irish company, not on CH) is missed; her
  vote is FOR either way. Reading the issuer's own board page when its notice has no biographies
  would close this.
- **Bonds never found count as not listed.** Research found no listed bonds for Bank of Scotland,
  so its seat isn't counted, though gold cites them. No vote changes: the Lloyds group already
  counts for Sarah Legg.
- **A rollback restores votes, not decisions.** Decisions recorded after the run you roll back to
  still replay on the next run; undo them with `pennant undo`. Rolling back past a run published
  after a decision shows that decision as the cause in `pennant diff`, though it is still active.
