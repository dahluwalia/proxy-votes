# Research prompts

Prompts sent to the research model (`classify/backends.py` picks the provider and model
from `LLM_MODEL`). Organisations are researched on the open web; a person's role only on
the firm's own website (`site_v1` finds it, `position_v2` runs with the search limited to it). Code loads each section by its `## name` heading, fills the
`{placeholders}`, and sends the fenced text exactly as written.

- **Versioned.** The section name (`org_v1`) is in the web cache key; the text is not.
  A wording fix that shouldn't change answers can keep its name. A change that should
  re-ask every question gets a new name (`org_v2`), set in `research.PROMPT_VERSION`.
- **Untrusted answers.** Whatever the prompt says, `websearch.validate` keeps a fact
  only if it quotes a page the search returned in the same response.

## org_v1

Asked once per organisation that rules and Companies House leave open: is it listed on
the AGM date, on which market, and is it a subsidiary. Placeholders: `{org}`, `{as_of}`
(the AGM date), `{today}`.

````text
You are checking facts about one organisation for a shareholder-voting analyst. The analyst counts a director's seats on the boards of listed companies, so a wrong answer can cause a wrong vote. An honest "unknown" is always better than a guess.

Search the web; do not answer from memory. Today is {today}. The question is about {as_of}, the date of an annual general meeting, and every answer must be true on that date. Listings, delistings, demergers and takeovers change, so look for sources dated as close to {as_of} as you can.

Organisation: {org}

Answer these questions:

1. Identify the organisation: its full legal name, its country of incorporation, and its type: company, charity, public_body, education, partnership or other. If the name could mean more than one organisation (a company and its foundation, a group and one of its subsidiaries), answer for the entity with exactly this name, and say which one you chose in "note".
2. Were its equity shares admitted to trading on a stock exchange on {as_of}? Give the exchange and the ticker. Say whether the market is a main market, AIM, another growth market (such as Euronext Growth or Aquis), or other. For a London Stock Exchange company, check whether it is on the Main Market or on AIM: company websites and RNS announcements say "AIM: <ticker>" or "admitted to trading on AIM". Say separately whether it has debt securities (bonds, notes) admitted to trading. A company with only listed bonds has equity_listed "no" and debt_listed "yes".
   Answer "no" when a source shows the shares were not traded on {as_of}: a completed takeover, a cancellation of admission to trading, a statement that it is privately owned, or a company register entry showing a private company. A wholly owned subsidiary's own shares are not listed, so its equity_listed is "no".
3. List listing, delisting, demerger and takeover events in the three years before {as_of}, with dates.
4. Is it a subsidiary of another organisation? If so, name the ultimate parent company and say whether the parent's equity shares were listed on {as_of}. Joint ventures have no single parent: answer is_subsidiary "no" and name the owners in "note".
5. Prefer stock exchange websites, investor-relations pages, regulator filings and major financial press over aggregators. For every answer give a URL that appeared in your web search results and a short quote (under 30 words) copied verbatim from that page. If sources conflict, or none answers the question, the value is "unknown" (or null for names, dates and tickers). Never fill a field from memory.

Reply with one JSON object in a ```json code block and nothing after it. Use exactly these keys. Every fact is an object with "value", "url" and "quote"; use null for "url" and "quote" when the value is unknown. A "value" holds only the answer itself: a bare name such as "Severn Trent Plc", a country such as "United Kingdom", or yes / no. Put any explanation in "note", never in a value.

```json
{
  "legal_name": {"value": "string or null", "url": "...", "quote": "..."},
  "country": {"value": "country of incorporation, or null", "url": "...", "quote": "..."},
  "org_type": {"value": "company | charity | public_body | education | partnership | other | unknown", "url": "...", "quote": "..."},
  "equity_listed": {"value": "yes | no | unknown", "url": "...", "quote": "..."},
  "exchange": {"value": "string or null", "url": "...", "quote": "..."},
  "ticker": {"value": "string or null", "url": "...", "quote": "..."},
  "market_segment": {"value": "main | aim | growth | other | unknown", "url": "...", "quote": "..."},
  "debt_listed": {"value": "yes | no | unknown", "url": "...", "quote": "..."},
  "is_subsidiary": {"value": "yes | no | unknown", "url": "...", "quote": "..."},
  "parent_name": {"value": "string or null", "url": "...", "quote": "..."},
  "parent_listed": {"value": "yes | no | unknown", "url": "...", "quote": "..."},
  "events": [{"date": "YYYY-MM-DD", "event": "short description", "url": "...", "quote": "..."}],
  "note": "optional short explanation"
}
```
````

## site_v1

Asked once per organisation before any role question about it: what is its own website.
`positions.research_site` keeps the answer only if the cited page is on the named site,
names the organisation, and the site is not a third-party one. Placeholders: `{org}`,
`{number}` (its Companies House number, or "not known").

````text
You are finding the official website of one organisation for a shareholder-voting analyst, who will then read only that organisation's own pages about its board. A wrong website would let someone else's pages decide a vote, so an honest "unknown" is always better than a guess.

Search the web; do not answer from memory.

Organisation: {org}
UK Companies House number: {number}

1. Find the website the organisation runs itself: for a company, its corporate or investor-relations site; for a subsidiary, the website of the group it belongs to, if that site covers it. A news site, encyclopedia, social network, stock exchange, market-data site, company registry or directory is never the organisation's own website.
2. Give the website's address, and a page on that website, from your search results, that names the organisation, with a short quote (under 30 words) copied verbatim from that page that names it.
3. If you cannot find a website clearly run by the organisation or its group, the value is "unknown".

Reply with one JSON object in a ```json code block and nothing after it. Use exactly these keys. "value" holds only the website address, such as "https://www.example.com"; put any explanation in "note".

```json
{
  "website": {"value": "https://... or unknown", "url": "a page on that website", "quote": "..."},
  "note": "optional short explanation"
}
```
````

## position_v2

Asked once per seat that could count but whose board seat, chair or executive fact is
still unknown, with the search tool limited to the firm's own website (`{domain}`).
`websearch.validate` also drops any fact citing a page off that site. The question is about
the AGM date: the vote must be in by then, so a later change doesn't matter. Placeholders:
`{person}`, `{org}`, `{role_text}`, `{as_of}` (the AGM date), `{today}`, `{domain}`.

````text
You are checking one person's position at one organisation for a shareholder-voting analyst. The analyst counts a director's seats on company boards, so a wrong answer can cause a wrong vote. An honest "unknown" is always better than a guess.

Your search is limited to {domain}, the organisation's own website, and only its pages count. Do not answer from memory or from any other source. Today is {today}. The question is about {as_of}, the date of an annual general meeting.

Person: {person}
Organisation: {org}
Role as recorded in the AGM notice or at Companies House: {role_text}

Answer these questions:

1. On {as_of}, was {person} a member of {org}'s board of directors? If so, as an executive director, a non-executive director (a senior independent director is one), the chair, or an executive chair? If the website shows the board of the group {org} belongs to, answer for that board and say so in "note".
2. Did the role begin or end within six months of {as_of}? Give the dates the website states.
3. A job title (CEO, CFO, COO, president, EVP, SVP, managing director, partner, senior adviser) is not evidence of a board seat: many people with these titles are not directors. Answer "yes" only when a page lists the members of the board (a board-of-directors or governance page, an annual report, a results announcement) and includes the person. Answer "no" only when such a list, current on or near {as_of}, does not include the person.
4. The website may show the board as it is today, not on {as_of}. Use the dates it gives; if the person's role may have changed between {as_of} and today, say so in "note".
5. For every answer give a URL that appeared in your search results and a short quote (under 30 words) copied verbatim from that page. If the website does not answer the question, the value is "unknown" (or null for dates).

Reply with one JSON object in a ```json code block and nothing after it. Use exactly these keys. Every fact is an object with "value", "url" and "quote"; use null for "url" and "quote" when the value is unknown. A "value" holds only the answer itself; put any explanation in "note".

```json
{
  "board_member": {"value": "yes | no | unknown", "url": "...", "quote": "..."},
  "capacity": {"value": "executive_director | non_executive_director | chair | executive_chair | unknown", "url": "...", "quote": "..."},
  "start_date": {"value": "YYYY-MM-DD, YYYY-MM or null", "url": "...", "quote": "..."},
  "end_date": {"value": "YYYY-MM-DD, YYYY-MM or null", "url": "...", "quote": "..."},
  "note": "optional short explanation"
}
```
````
