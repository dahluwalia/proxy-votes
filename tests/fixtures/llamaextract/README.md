# LlamaExtract recordings (M1.1)

One file per notice, named after the PDF. Each is the raw response of the single live
LlamaExtract run for that notice (agentic tier, `per_doc`, `cite_sources: true`; the pinned
version date resolved to extract 2.6), plus what it was recorded under:

```
{"doc", "pdf_sha256", "schema_version", "schema_sha", "config", "cache_key", "recorded_at",
 "response": <ExtractV2Job JSON, fetched with expand=extract_metadata,usage,configuration>}
```

- **Read:** `pennant.extract.llamaextract.extract_notice(pdf, ctx)` returns a `NoticeExtract`.
  A recording whose `cache_key` matches the current (PDF, schema version, config) is copied into
  the `llamaextract` cache, so a fresh database never calls LlamaExtract again.
- **Citations** sit under `response.extract_metadata.field_metadata.document_metadata`, keyed by
  field; arrays are lists by position. Each leaf has `citation: [{page, matching_text, ...}]`
  with 1-based pages. `matching_text` comes from LlamaExtract's markdown parse (it can contain
  `**`), so grounding uses the verbatim `quote` field instead.
- **Budget:** about 25 credits a page; the five runs cost about 1,775 credits. Changing the
  schema or config (including the system prompt in `pennant/extract/prompts.md`) changes the cache key, and the next live call is refused (`BudgetError`)
  until expert approves a re-run: `.venv/bin/python -m pennant.extract.llamaextract record --force
<pdf>`. A job that outlives polling can be fetched without re-running: `record --job <id>`.
