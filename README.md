# Who really sits on which boards

> "I can't trust what the AGM notices say about directors' other roles, and the registers don't
> line up with them either. I don't want to vote against the wrong person. Can you help?"

`pennant` is a pipeline and command-line tool for that analyst. For every director standing for
election at a UK AGM, it finds their board seats in the notice and on Companies House (CH), scores
the board-commitment rule and recommends **FOR**, **AGAINST** or **REVIEW**. Every fact cites its
source: a notice page, a CH record or a web page.

## How I scoped it

- **The notice is a claim, and the register is a check.** Each seat is reconciled with CH as of the
  AGM date: confirmed, stale (resigned before the AGM), contradicted, or undisclosed (on CH but
  missing from the notice).
- **People are matched through companies, not names.** Each notice name is matched against the
  issuing company's own CH officer list. That record's date of birth then links the person's other
  CH records.
- **A wrong AGAINST is the worst mistake.** Every fact is yes, no or unknown. A vote that depends
  on an unknown fact is REVIEW, and names the fact that would settle it.
- **Rules first, models only where needed.** LlamaExtract reads the notices. Claude with web search
  answers whether an organisation is listed and who its parent is, and looks up a person's role only
  on their firm's own website. Plain code does the rest.
- **In scope:** only directors standing for (re)election, and only the points and executive rules.

How the policy's grey areas are read (each is a flag in `config/policy.yaml`):
- AIM counts as listed.
- A company with only bonds listed counts.
- A chair-designate isn't chair until appointed: roles are measured on the AGM date.
- One corporate group counts once, so a company's own subsidiaries add nothing.

## What the analyst can do

| The brief asks her to | Command |
| --- | --- |
| See one record per real person, with the document and page behind every link | `pennant show "Chris Kennedy"`: both notices, his CH records, each seat with its citation |
| See each director's points and recommended vote | `pennant votes --meeting tesco` |
| Merge or split records, undo, and see what changed | `pennant merge`, `split`, `set-fact`, `undo`. Each asks first, re-runs and prints the diff naming the decision; see also `pennant history <name>` and `pennant decisions` |
| Be told when the system isn't sure | REVIEW votes, and `pennant review`: each open question with the command that settles it |

Runs are snapshots: `pennant runs`, `pennant diff`, `pennant rollback run:N`. The
[user guide](docs/user-guide.md#demo) walks through all of this on the five notices.


## Results

Graded with `pennant eval` against the gold set ([how it was built](eval/gold/README.md)). The gold
expects 41 FOR, 7 AGAINST and 1 REVIEW; one more resolution was withdrawn before its AGM and isn't
scored.

| Run | Wrong AGAINST | Wrong FOR | Accuracy | REVIEW | Identity (pairwise P/R) | Countable seats (P/R) |
| --- | --- | --- | --- | --- | --- | --- |
| Live, published (`run:2`) | 0 | 0 | 90% | 10% | 100% / 100% | 100% / 96% |
| Offline, from recordings | 0 | 0 | 94% | 6% | 100% / 100% | 99% / 96% |

- **People:** 50 notice mentions become 47 people (Chris Kennedy, Sharmila Nebhrajani and Tamara
  Ingram each stand twice), and all 47 match a CH record.
- **REVIEWs:** each is a fact neither the notice, CH nor the firm's own site settles, and each names
  that fact. One example is whether Kawal Preet, FedEx's EVP, sat on its board on the AGM date.

The full report is [`eval/report.md`](eval/report.md), with a line for each of 17 hard cases.

## Quick start

Needs Python 3.12. The offline run replays recorded answers, so it needs no keys.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -m "not live"
.venv/bin/pennant run --offline --publish
.venv/bin/pennant votes --meeting tesco
#.venv/bin/pennant show "Caroline Silver"
```

For a live run, copy `.env.example` to `.env`, add the CH, LlamaCloud and Anthropic keys, then run
`.venv/bin/pennant run --publish`. Notices go in `data/notices/`; any UK AGM notice works, and its
meeting key comes from the filename (`Marks_and_Spencer_2026_Notice_of_AGM.pdf` →
`marks-and-spencer`).

## Documentation

| Read | For |
| --- | --- |
| [User guide](docs/user-guide.md) | setup, every command, the nightly job and rollback, logs, evaluation, the demo, known gaps |
| [Technical guide](docs/technical-guide.md) | architecture, database schema, stage interface, caching, logging, testing, extending |
| [Pipeline diagrams](docs/pipeline.html) | an overview, one diagram per stage, and the database (open in a browser) |
| [`eval/gold/README.md`](eval/gold/README.md) | how the gold set was built from cited evidence |
