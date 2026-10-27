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
(`logs/pennant-run-20.log`; `PENNANT_LOG_DIR` overrides the folder): each stage's start, end,
time and counts, every error with its traceback, each live LlamaExtract job and research call
with its tokens, Companies House retries and outages, and the gate results. The run's report
also keeps the counters, so `pennant log` works for any run:

```text
$ pennant log run:20
run:20: published  by system  started 2026-10-27T14:12:27+00:00  finished 2026-10-27T14:12:28+00:00

Stages
Stage           Result  Time  Counts
─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
stage extract   ok      1.1s  meeting=5, review=0, roster=50, seat=230, ungrounded=0
stage ch        ok      0.0s  appointments_unavailable=0, issuers=5, issuers_ok=5, officers=49, recently_resigned=5
stage dedup     ok      0.1s  links=152, mentions=50, people=47, reviews=8, skipped_ch_unavailable=0
stage classify  ok      0.1s  listed_by_rule=117, org_research=81, orgs=285, position_research=33, position_targets=37, seats=230
stage score     ok      0.1s  AGAINST=8, FOR=36, REVIEW=6, recommendations=50, review_items=31, seats=259

Work
Source        Counts
─────────────────────────────────────────────────────────
cache         ch.hit=509, llamaextract.hit=5, web.hit=144
LlamaExtract  credits_cached=1775, jobs_cached=5
PDF           pages_read=130

AI usage (research model)
Question           Calls  Input tokens  Output tokens  Searches
───────────────────────────────────────────────────────────────
org (cached)          81     5,519,976        229,955       342
position (cached)     33     1,455,062         45,929        73
site (cached)         30       825,026         16,277        55
total (cached)       144     7,800,064        292,161       470
live = spent by this run; cached = what the reused answers cost
when they were fetched

  log file: logs/pennant-run-20.log
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

The backfill copied in the 217 answers cached before the record existed (all three versions of
the role prompt among them), with their responses, tokens and validation but no prompt text.

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
- **First night (27 Oct, `run:7`):** published with every gate passing and no vote change;
  352 of 432 CH answers were identical. The other 80 were officer searches that CH had only
  reordered, which is what led to comparing searches by namesakes; a full refetch of all 45
  searches since then added no rows.

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

A held run lists each failing gate's problems; demo step 5 shows one.

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
| `pennant merge <a> <b> -r "why"` | these are one person (must-link); `a` and `b` are names, person or mention ids, or a CH officer id, as review items suggest |
| `pennant split <person> --move <mention or meeting key> -r "why"` | these mentions are someone else (cannot-link) |
| `pennant set-fact <seat_id \| org_key \| mention_id> <field> <value> -r "why"` | analyst override of one fact |
| `pennant undo <dec:N>` | revoke a decision |
| `pennant diff [run_a] [run_b]` | compare two runs (default: published vs latest) |
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
`yes`, `no` or `unknown`. The `Would flip the vote:` line in `show` gives targets in
exactly this form (`seat:00445790:r7:1.board_seat`).

`review` output:

```text
$ pennant review
41 review items  [run:1]   7 kinds: not_in_office, possible_same_person, site_not_found, stale, unanchored, undisclosed, web_unsourced
  not_in_office  (1)
    [not_in_office] nm:00445790:r7: Thierry Garnier matches GARNIER, Thierry Dominique Gerard (WnytyEoBEEno7UhOqQ-tsXfC5zk), who was not a director of 00445790 on 2026-06-18 GARNIER, Thierry Dominique Gerard (WnytyEoBEEno7UhOqQ-tsXfC5zk) was a director 2021-04-30 to 2026-05-06; the notice's seat is checked against those dates
        -> pennant show "Thierry Garnier"
  …
    [undisclosed] seat:NtEjRLqBMJFlrhE0-U3mm8wh_C8:00053723: p:nm:04256886:r7: CH shows director of PEARSON PLC on 2026-07-07; not in the notice
        -> pennant set-fact seat:NtEjRLqBMJFlrhE0-U3mm8wh_C8:00053723 board_seat <yes|no> --reason "..."
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
vote accuracy, REVIEW rate, pairwise identity precision and recall, countable seats,
the result under each `aim_counts_as_listed` value, and a line per hard case H1–H17.
A meeting the gold set doesn't cover is listed, not graded. The gold set, its
sources and the scorer are described in [`eval/gold/README.md`](../eval/gold/README.md).

### Results (27 Oct 2026)

Graded against the gold set as rebuilt from cited evidence ([`eval/gold/README.md`](../eval/gold/README.md)):
41 FOR, 7 AGAINST, 1 REVIEW, and Garnier's withdrawn resolution, which isn't scored. Policy as in
`config/policy.yaml`: AIM counts as listed, bond-only listings count, a chair-designate isn't chair.

| Run | Wrong AGAINST | Wrong FOR | Accuracy | REVIEW | Identity (pairwise P/R) | Countable seats (P/R) |
| --- | --- | --- | --- | --- | --- | --- |
| Live, published (`run:19` in `pennant.db`) | 0 | 0 | 90% | 10% | 100% / 100% | 99% / 96% |
| Offline, fresh database | 0 | 0 | 92% | 8% | 100% / 100% | 99% / 96% |

50 roster mentions become 47 people; 46 anchor to Companies House. Every disagreement with gold is a
REVIEW, never a wrong vote. Companies House records every seat as plain "director", so the capacity
(chair or not, executive or not) comes from the notice or, failing that, the firm's own website: the
M&S roles (its notice has no biographies), and the CH-only seats of Ashton, Cooke, Cosslett, Ewing and
Nebhrajani, are settled that way. Banks and a group's finance company often list only bonds (Lloyds
Bank, Bank of Scotland, HSBC UK Bank, Tesco Corporate Treasury Services); with bond-only listings counting
they are listed seats, but each shares a group with another seat of the same director, so it adds no
points. The REVIEWs left: Kawal Preet (FedEx's site doesn't say whether she was on its board on the AGM
date), Tom Delay ("Tom" vs CH "Thomas" is below the name-match bar), Graham Allan (Bata's listing
unsourced), Laura Crespi (no own website found for Youngever) and, live only, Sarah Legg (HBOS's bonds
count, but no HBOS site of its own was found to give her role there). Each REVIEW names the fact that
would settle it, ready for `pennant set-fact`.

## Demo

Offline, on a fresh database, so no keys are needed and decision ids match:

```bash
export PENNANT_OFFLINE=1 PENNANT_DB=/tmp/pennant-demo.db
rm -f $PENNANT_DB
.venv/bin/pennant run --offline --publish
```

**1. Tesco votes.** 11 directors. Chris Kennedy is AGAINST under the executive rule (ITV CFO and
executive director, plus this seat). Caroline Silver is FOR at 4 points: Moelis's own governance
page doesn't list her among its directors. Thierry Garnier is REVIEW: CH shows him leaving the Tesco
board on 6 May 2026, before the AGM (Tesco withdrew his re-election), so his Tesco seat is stale.
Stewart Gilliland is REVIEW on the AIM flag.

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
```

**2. Caroline Silver.** `show` lists each seat with its notice citation, the CH appointment that
confirms it, the cited web sources behind each organisation's listing, and, for a role the notice
leaves open, the firm's own page:

```text
$ pennant show "Caroline Silver"
Caroline Silver  (p:nm:00445790:r13)  [run:1]

Vote at Tesco: FOR   4 points   points rule no · executive rule no
  • Tesco PLC (Independent Non-executive Director): 1 pt [confirmed]
  • Barratt Redrow plc (Chair): 2 pt [confirmed]
  • Intercontinental Exchange, Inc. (Non-executive director): 1 pt [ch_invisible]
  • points 4 > 4: no; non-executive here: yes
  • executive rule: no

…
Seats
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
✓ yes   ✗ no   ? unknown

Sources
  Barratt Redrow plc   seat:00445790:r13:1
    Tesco notice p.10: "Chair: Barratt Redrow plc and ICE Clear Europe"
    Companies House: https://find-and-update.company-information.service.gov.uk/officers/sYzrAgP05-XBMuExY4hjznwnRxc/appointments — "BARRATT REDROW PLC: director, appointed 2023-06-01" (updated 2026-10-26)
    Barratt Redrow plc: listed yes on London Stock Exchange (BTRW)
…
```

ICE Clear Europe scores nothing: CH says it is a private company, and research names
Intercontinental Exchange as its parent.

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
run:1    published  2026-10-27 16:41  REVIEW  1–2     exec rule?  first run
run:2    published  2026-10-27 16:41  FOR     1–1     -           dec:1
run:3 *  published  2026-10-27 16:42  REVIEW  1–2     exec rule?  dec:2
* the published run (run:3)

Changes to their records (person, seats, organisations)
  run:1 → run:2  seat seat:04267576:r13:5 (FedEx): board_seat unknown → no  (dec:1)
  run:2 → run:3  seat seat:04267576:r13:5 (FedEx): board_seat no → unknown  (dec:2)

Decisions about this director
Id     Made              By       Decision                                      Reason
─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
dec:1  2026-10-27 16:41  analyst  set seat seat:04267576:r13:5 board_seat = no  FedEx's board page doesn't list her: an EVP, not a director  revoked by dec:2
dec:2  2026-10-27 16:41  analyst  undo dec:1                                    undo
```

**5. Chris Kennedy, one person across two notices.** Both mentions anchor to the same CH officer
(Christopher John KENNEDY, born 1/1964, director of Tesco and ITV), so `show` gives one person with
an AGAINST at each meeting. Split him, then undo:

```text
$ pennant split "Chris Kennedy" --move itv --reason "test: treat the ITV mention as someone else" -y
Recorded dec:3: cannot_link {'a': ['nm:04967001:r12'], 'b': ['nm:00445790:r9']}
run:4: published
…
p:nm:00445790:r9 split into p:nm:00445790:r9, p:nm:04967001:r12 (dec:3)
  Chris Kennedy @ itv: vote AGAINST → REVIEW; points 2–2 → 1–3; exec_rule yes → unknown (dec:3)

$ pennant undo dec:3 -y
Recorded dec:4: revoke dec:3
…
p:nm:00445790:r9 merged with p:nm:04967001:r12 (dec:4)
  Chris Kennedy @ itv: vote REVIEW → AGAINST; points 1–3 → 2–2; exec_rule unknown → yes (dec:4)
```

A decision the pipeline can't honour holds the run instead. Ken and Gerry Murphy both stand at
Tesco, so they can't be one person:

```text
$ pennant merge "Ken Murphy" "Gerry Murphy" --reason "test" -y
Recorded dec:5: must_link {'a': 'nm:00445790:r11', 'b': 'nm:00445790:r10'}
run:6: held
  G1 pass
  G2 pass
  G3 skipped
  G4 FAIL
    - dec:5: nm:00445790:r11 and nm:00445790:r10 are not one person
  Not published; votes still come from run:5.
```

`pennant undo dec:5 -y` clears it.

**6. Nightly safety (H16).** With CH unavailable for one issuer, the run is held and the published
votes don't change:

```bash
.venv/bin/pytest -k h16     # 6 passed
```

**7. Evaluation.** `pennant eval` writes `eval/report.md` (see [Evaluation](#evaluation)).

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
- **Unanchored directors** (Tom Delay) are scored without CH until an analyst links them:
  `pennant merge nm:02366619:r9 3foiCz6Hg_2s4JTbIRFd01JBwG4 -r "..."`.
