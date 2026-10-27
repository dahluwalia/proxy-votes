# Page-text fixtures (M2.1)

One JSON file per notice, named after the PDF: `{"doc": "<pdf filename>", "pages": [...]}`.
`pages[0]` is page 1. The text is PyMuPDF `page.get_text("text")`, unmodified.

- Load: `pennant.evidence.grounding.load_pages("tesco")` (meeting key or PDF filename).
- Ground a quote: `ground(page_text, quote)` or `ground_near(pages, cited_page, quote)`.
- Regenerate: `.venv/bin/python -m pennant.evidence.grounding`.
