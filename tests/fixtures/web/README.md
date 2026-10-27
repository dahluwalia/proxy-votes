# Web research fixtures (M5.2)

Recorded answers from Claude with the web search tool (`web_search_20250305`, `max_uses: 5`,
model `claude-sonnet-5-5`), one file per question:

- `org__*` (`org_v1`): an organisation's listing, market and parent, from the open web.
- `site__*` (`site_v1`): the organisation's own website.
- `position__*` (`position_v2`): one person's role at one organisation on the AGM date, searched with
  `allowed_domains` set to that organisation's own site. Its file name ends in the site.

Each file is `{"key": <cache key>, "payload": {...}}`, where `payload` is exactly what the
`web` cache stores:

- `kind`, `prompt_version`, `inputs`: the question. `inputs` holds the normalised cache-key
  inputs: `org_key` and `as_of` (the AGM date); a role question adds `person` and `domain`.
- `content`: the search queries, every search result returned (`url`, `title`, `page_age`)
  and the model's final text with its JSON answer. Encrypted page content and thinking are
  dropped.
- `model`, `stop_reason`, `usage` (tokens and searches), `fetched_at`.

Validation runs on read (`classify.websearch.validate`), so changing the rules never needs a
re-recording. A fact counts only when its URL is one of the returned results and it carries a
quote, and a role fact only when that page is on the firm's own site.

**Use offline:** `classify.websearch.load_fixtures(conn)` seeds the cache; then
`research_org(name, as_of, ctx)` with `ctx.offline=True` answers from it.

**Record more:** run `research_org` (or the classify stage) with
`offline=False` against a database, then `classify.websearch.export_fixtures(conn)`. Cost is
about $0.10–0.20 per question.

| File | Case |
| --- | --- |
| burberry-group, burberry-foundation | H8: a listed group and its charity are different orgs |
| ice-clear-europe, intercontinental-exchange | H8, H12: subsidiary (listed no) → parent ICE (NYSE) |
| hafren-dyfrdwy, water-plus | H8: Severn Trent subsidiary; a 50/50 joint venture has no parent |
| greenhill-and-co-international | H8: LLP, parent Mizuho |
| ocean-outdoor, keywords-studios | H9: taken private (2022, 2024) → listed no |
| magnum-ice-cream-company | H9: listed on Euronext Amsterdam since the Unilever demerger |
| capri-holdings, americana-restaurants-international, man-group | H9: NYSE (BVI); ADX/Tadawul (ADGM); LSE (Jersey) |
| ig-design-group | H13: AIM |
| lloyds-banking-group | H17: group parent |
| fedex; position fedex.com kawal-preet | H7: FedEx is NYSE-listed; fedex.com doesn't say whether its EVP Kawal Preet was a director (unknown, REVIEW). The search also returned SEC filings, which validation drops |
| moelis-and-company; position moelis.com caroline-silver | H12: Moelis is NYSE-listed; its directors page doesn't list Caroline Silver (board seat no, FOR at 4) |
| position corporate.marksandspencer.com | H11: the M&S notice has no biographies; M&S's own site gives each director's role |
| bata-international | H12: Graham Allan's chair seat. Two searches found no source, so it stays unknown (REVIEW) |
| lloyds-bank, bank-of-scotland, hbos | H17: CH-only Lloyds boards (Legg), parent Lloyds Banking Group |
| every other file | the rest of what the classify stage asks on the committed LlamaExtract and CH fixtures: notice orgs, the five issuers, and CH-only directorships |

`tests/classify/test_research_fixtures.py::test_full_offline_chain_is_fully_recorded` runs
extract → CH → dedup → classify offline and fails if any question lacks a recorded answer.
