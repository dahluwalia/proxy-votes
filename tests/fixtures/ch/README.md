# Companies House fixtures

These are recorded responses from the live Companies House API, made by
`scripts/record_ch_fixtures.py` (card M3.2). Each file holds one CH cache row per URL:

```json
{"<API URL>": {"status": 200, "body": {...raw CH JSON...}, "fetched_at": "2026-10-26T..."}}
```

`status` is 200 or 404, the only responses the client caches. Failures are never recorded.

Bodies are raw CH JSON, except that search hits keep only `title`, `links`,
`date_of_birth`, `description` and `appointment_count`: the fields the client reads, plus
a readable summary. Dropping addresses and match snippets cut `search.json` from about
7 MB to 3 MB.

| File | Endpoint |
| --- | --- |
| `companies.json` | `GET /company/{number}`: profiles (name, type, status) |
| `officers.json` | `GET /company/{number}/officers`: the five issuers' officer lists, every page |
| `appointments.json` | `GET /officers/{id}/appointments`: every page |
| `search.json` | `GET /search/officers?q=<CH name>`: the first two pages, one row per page, slimmed (see above). `load_fixtures` joins each name's pages into the one cache entry per search that the client reads |

## What was recorded

- The five issuers' profiles and full officer lists (ITV, Intertek, Tesco, M&S, Severn Trent).
- For every director in office on their issuer's AGM date, and every director who
  resigned in the year before it (e.g. Thierry Garnier, who left Tesco on 2026-05-06):
  - their appointments
  - an officer search on their CH name
  - the appointments of each search hit with the same DOB and an exact name
  - company profiles for every appointment still live on or after 2026-05-07, the earliest AGM
- For the hard-case people (Kennedy, Nebhrajani, Ingram, Garnier, Hampton, Miles,
  Allen, Legg, Silver, Gilliland, Ken Murphy, Gerry Murphy): company profiles for every
  company in their appointment lists, resigned ones included.

- Added in wave 2, one at a time (not a re-record): the appointments of two same-DOB records
  that `expand()` now links, Sharmila NEBHRAJANI OBE (`-A0-iiopZsWl7Bz6c5gP0sKHOk8`) and
  Carolyn MCCALL DBE (`7NK8UpcMxJj8JuQLvlgw0pS-oV4`), fetched through `client.appointments`.

So `client.run(ctx)` (the CH stage) and `dedup.anchor.expand()` both run fully offline
on these fixtures.

## Using them in a test

```python
from pennant.ch import client

client.load_fixtures(ctx.conn)   # seeds the `ch` cache; the ctx fixture is offline
client.officers("00445790", ctx)  # -> CHResult(status="ok", data=[CHOfficer, ...])
```

Anything not recorded comes back `unavailable` offline. That is correct behaviour, not
a fixture bug.

## Re-recording

```bash
.venv/bin/python scripts/record_ch_fixtures.py
```

This needs a Live `CH_API_KEY` in `.env`; a sandbox (Test) key gets 401 from the live API.
The script seeds its cache from the existing files, so it only fetches what is missing.
To refresh everything, delete the JSON files first. At 2 requests a second a full
recording takes a few minutes.
