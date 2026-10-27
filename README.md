# Board Commitments Pipeline

For each director standing for election at a UK AGM, `pennant` recommends **FOR**,
**AGAINST** or **REVIEW** under the board-commitment policy, with a cited source for
every fact (notice page, Companies House URL or web page).

- **Input:** AGM notice PDFs in `data/notices/`. The five 2026 notices there (ITV,
  Intertek, Marks and Spencer, Severn Trent, Tesco) are examples; any notice works.
- **Meeting key** comes from the filename: `Marks_and_Spencer_2026_Notice_of_AGM.pdf`
  → `marks-and-spencer`.
- **How:** LlamaExtract reads the roster and appointments; Companies House (CH)
  anchors each director and finds seats the notice left out; Claude with web search
  settles organisations' listed status and parents, and a person's role only from the
  firm's own website; plain code scores the policy. Both models can be swapped (see
  [the technical guide](docs/technical-guide.md#swapping-a-model)). Anything that could
  flip a vote goes to REVIEW, with the facts that would settle it.

## Documentation

| Read | For |
| --- | --- |
| [User guide](docs/user-guide.md) | setup, running, commands, the nightly job, logs, evaluation, the demo, known gaps |
| [Technical guide](docs/technical-guide.md) | architecture, data model, stage interface, caching, logging, testing, extending, layout |
| [Pipeline diagrams](docs/pipeline.html) | an overview and one diagram per stage (open in a browser) |
| [`CLAUDE.md`](CLAUDE.md) | the build spec, step cards and hard-case catalog |
| [`eval/gold/README.md`](eval/gold/README.md) | how the gold set was built from cited evidence |

## Quick start

Needs Python 3.12.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env                                  # keys only needed for live runs
.venv/bin/pytest -m "not live"                        # offline tests, no keys
.venv/bin/pennant run --offline --publish             # from recorded answers, ~2 s
.venv/bin/pennant votes --meeting tesco
.venv/bin/pennant run --publish                       # live: LlamaExtract, Companies House, Claude
```

The [user guide](docs/user-guide.md#setup) lists every `.env` setting and command.

## Results (27 Oct 2026)

Graded against the gold set as rebuilt from cited evidence ([`eval/gold/README.md`](eval/gold/README.md)):
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
