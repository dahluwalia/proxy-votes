# Gold set: board commitments, five 2026 AGMs

Hand-labelled truth for every director standing at the ITV, Intertek, Tesco, M&S and Severn Trent
2026 AGMs, plus a scorer that grades pipeline output against it. The headline metric is
**wrong AGAINST votes** (target 0).

## What's in it

| File | Rows | What it holds |
| --- | --- | --- |
| `meetings.yaml` | 5 | company number, AGM date, meeting id (`mtg:<number>:<date>`) |
| `roster.yaml` | 50 | one per (re)election resolution: key (`Tesco#7`), mention id (`nm:00445790:r7`), person, role at the meeting, coverage, resolution and bio PDF pages |
| `persons.yaml` | 51 | 47 standing people (3 appear in two notices) + 4 named but not standing |
| `seats.yaml` | 347 | every seat with its facts, `evidence` (notice line, CH appointment, cited page) and `listed_evidence`: 236 from the notices, 103 from Companies House, 8 from published board biographies |
| `votes.yaml` | 50 | expected vote, points interval, rule fired, counted seats, policy sensitivity, fragile facts |
| `source/director_positions.xlsx` | | the spreadsheet of roles read from the notices: the place to edit roles |
| `overrides.yaml` | | facts the spreadsheet doesn't carry: meetings, roster, organisations, researched seats, row fixes, withdrawn resolutions |
| `evidence.yaml` | | the source behind every label: CH records per person, notice quotes, role and organisation sources |
| `build_gold.py`, `evidence.py` | | rebuild everything above; the build fails if any label lacks evidence or a hard case moves |
| `score.py` | | grades a pipeline's CSV export; `--selftest` proves it catches injected errors |
| `hard_cases.yaml` | 17 cases | the CLAUDE.md H1–H17 checks, run by `eval/hard_cases.py` against a stored run |

Expected votes: **41 FOR, 7 AGAINST, 1 REVIEW**, and one resolution withdrawn before its AGM (49 scored).

| Mention | Director | Expected | Why |
| --- | --- | --- | --- |
| ITV#4 | Dawn Allen | AGAINST | Haleon CFO and director (CH), plus the ITV seat |
| ITV#12, Tesco#9 | Chris Kennedy | AGAINST at both | ITV CFO/COO and executive director, plus the Tesco seat |
| ITV#13 | Carolyn McCall | AGAINST | ITV CEO, plus a Bridgepoint Group NED seat (CH; not in the ITV notice) |
| M&S#7 | Alison Dolan | AGAINST | M&S CFO, plus a Pearson NED seat (CH; not in the M&S notice) |
| Severn Trent#7 | Helen Miles | AGAINST | Severn Trent CFO, plus a Breedon NED seat |
| Severn Trent#8 | Nick Hampton | AGAINST | Tate & Lyle CEO and director (CH), plus the Severn Trent seat |
| Tesco#8 | Stewart Gilliland | REVIEW | IG Design is on AIM: AGAINST if AIM counts as listed, FOR if not |
| Tesco#7 | Thierry Garnier | (withdrawn) | he resigned from the Tesco board on 6 May 2026 and Tesco withdrew resolution 7 |

Ten votes sit at exactly 4 points, one under the threshold: Andrew Cosslett, Graham Allan, Tamara Ingram (at
both meetings), Melissa Bethell, Gerry Murphy, Caroline Silver, Fiona Dawson, Christine Hodgson and Sharmila
Nebhrajani. One false positive on any of their seats is a wrong AGAINST.

## How it was built

The same steps for every director. Each label in `seats.yaml` carries the evidence it was read from, and
`build_gold.py` refuses to build if a seat has none, or if a seat that could count rests on an organisation
with no source for its listing.

1. **Roster.** Every director election or re-election resolution in the notice, with its PDF page. A
   resolution the company withdrew before the AGM stays on the roster with no expected vote, citing the
   withdrawal (`overrides.yaml → withdrawn`, `evidence.yaml → withdrawn`).
2. **Identity.** The director's Companies House record is the one on the issuer's officer list with their
   surname and given name (a short form such as Chris for Christopher counts), in office or appointed by the
   AGM. Any other CH record with the same full name and the same birth month and year is the same person.
3. **Seats**, from three sources:
    - the notice: every appointment the biography states, with the line quoted (`notice_quotes`);
    - Companies House: every director appointment on the person's records that is live on the AGM date at a
      company CH shows as active;
    - where a notice has no biographies (M&S), the company's own published board biographies, quoted.

   A notice seat at a company CH shows is matched to that appointment, which supplies its dates.
4. **Seat facts.**
    - *Current*: the notice's tense; for a CH appointment, its dates against each meeting's AGM date (a seat
      that starts between two AGMs counts only at the later one). The seat at the meeting itself is the seat
      under vote and always counts (policy `count_seat_under_vote`).
    - *Board seat*: a CH director appointment is one. Otherwise the role title decides (the CLAUDE.md M5.1
      table); a job title alone (CEO, EVP) needs a cited board list.
    - *Chair and executive*: the role title, from the notice or a cited source.
    - *Listed*: a private company type on CH settles "no". Anything else needs a cited exchange, company or
      regulator page showing its shares traded on the AGM date (`listed`).
    - *Debt listed*: a company whose shares aren't listed may still have bonds admitted to trading (banks,
      a group's finance company). `debt_listed: yes` needs a cited page showing them listed; no current
      listing found after a search is `unknown` (HBOS); no bonds found is the default `no`. Such a seat
      counts as listed when policy `debt_only_listing_counts` is yes. Bond-listed here: Lloyds Bank,
      Bank of Scotland, HSBC UK Bank, Tesco Corporate Treasury Services; searches found none for British
      Airways, Intertek Finance, Ocean Outdoor, and Federal Express's certificates are not exchange-listed.
    - *Groups*: a subsidiary (parent from the CH register of persons with significant control, or a cited
      source) is a seat like any other, but every seat in one corporate group counts once (the best of them).
5. **Votes are computed, not typed in.** `build_gold.py` applies the policy with yes/no/unknown logic: 1 point
   per counted seat, 2 per chair; a corporate group counts once; non-executives are AGAINST above 4 points;
   executive directors of a listed company are AGAINST if they hold any other listed seat.
6. **Fragile facts.** For every vote, the build flips each label on each outside seat (board seat, listed,
   current) and records any flip that would change the vote (`votes.yaml → fragile_facts`). Each of those
   labels has its source in `seats.yaml`.

Web sources were opened and quoted verbatim; most London listings cite the LSEG market data on Barclays'
research pages, because the exchange's own pages don't render without JavaScript. Defaults: AIM counts as
listed `unknown`; a chair-designate counts as chair `unknown` (Steve Mogford is 3–4, FOR either way); a
bond-only listing counts `unknown`. The build stops if any vote depends on the bond flag (none does: every
bond-listed board above shares a group with another seat of the same director), and the scorer recomputes
gold points and countable seats under whatever flags it grades with (`--debt-only`, like `--aim`).

## Running it

From the repo root:

```bash
.venv/bin/pennant eval                                   # export the published run, grade it, write eval/report.md
.venv/bin/python eval/gold/build_gold.py                 # after editing the spreadsheet, overrides or evidence (needs openpyxl)
.venv/bin/python eval/gold/score.py --selftest           # sanity-check the scorer
.venv/bin/python eval/gold/score.py eval/out --aim yes   # re-grade the last export under a different policy flag
```

`pennant eval` exports the run into `eval/out/` (ignored by git) through `eval/export.py`, then calls
`score.py` with the flags in `config/policy.yaml`:

| File | Columns | Notes |
| --- | --- | --- |
| `roster.csv` | `mention`, `person_id` | mention = `nm:…` id or `Tesco#7` key; person_id = the pipeline's own cluster id |
| `votes.csv` | `mention`, `vote`, `points_lo`, `points_hi` | vote is FOR, AGAINST or REVIEW |
| `seats.csv` | `person_id`, `org`, `current`, `board_seat`, `chair`, `listed`, `counted` | optional; yes/no/unknown values |

## How it scores

- **Roster:** precision and recall of the 50 resolutions.
- **Identity:** pairwise precision and recall over mentions. False merges and missed merges are listed by name.
- **Votes:** a confusion matrix against gold, plus four counts:
    - **wrong AGAINST** (headline)
    - wrong FOR
    - overconfident: gold is REVIEW but the pipeline decided
    - deferred: the pipeline said REVIEW where gold decides

  REVIEW is never counted as wrong, only reported, so honest deferral beats guessing.
- **Seats:** precision and recall of countable seats. Each false positive is labelled with its cause:
    - past role counted
    - non-board role counted
    - subsidiary counted
    - unlisted org counted
    - not in gold

## Read this before trusting a score

- **Many counted seats are not in any notice** (`votes.yaml → counted_seats_not_in_any_notice`): the M&S
  outside roles, and Companies House seats such as McCall's Bridgepoint and Dolan's Pearson directorships.
  A notice-only pipeline should say REVIEW for those people, which scores as deferred, not wrong.
- **Roles the notice and CH don't state** (all M&S titles, Kawal Preet at FedEx, Caroline Silver at Moelis)
  come from cited company sources. The pipeline doesn't research people's roles, so it defers these.
- **Org matching.** The scorer matches org names fuzzily within each person, so Burberry Group and The
  Burberry Foundation stay apart. Abbreviations such as "ICE" for Intercontinental Exchange won't match.

## How the pipeline uses it

- **Nothing in the pipeline is tied to these five notices.** It runs on whatever PDFs are in `data/notices/`.
  This gold set labels the five example notices; any other notice in a run is listed in the report as
  "not in gold" and isn't scored. Gold mentions missing from a run count as missing, so a partial run's
  accuracy is out of the gold total, never inflated.
- **Seats** are exported with the facts scoring used (org listing and policy flags applied), so `counted`
  in `seats.csv` matches what earned points in the vote.
- **Policy sensitivity.** The report re-scores the run under each `aim_counts_as_listed` value (without
  writing) and grades each against gold's answer for that value.
- **Hard cases** (`hard_cases.yaml`) follow gold where they overlap. Where gold needed a role source the
  pipeline doesn't use (Silver at Moelis, Preet at FedEx), REVIEW is accepted as a deferral; Garnier's
  withdrawn resolution must never be AGAINST.
- **Rebuilding** needs `openpyxl` (in the dev extra) and the recorded CH responses in `tests/fixtures/ch/`;
  scoring needs only PyYAML. Rebuilding reproduces the shipped YAML byte for byte.
