# Evaluation report — run:7

Generated 2026-10-27 20:22 UTC under `config/policy.yaml`.

## Headline

- **Wrong AGAINST votes: 0** (target 0)
- Wrong FOR votes: 0
- Vote accuracy: 90% on 49 gold-scored entries; REVIEW rate 10%; points interval exact 84%
- People, pairwise: precision 100%, recall 100%
- Countable seats: precision 100%, recall 98%
- Gold covers 50 of 50 roster entries in this run
- 50 roster mentions → 47 people; 47 anchored to Companies House

## Votes by meeting

| Meeting | Directors | FOR | AGAINST | REVIEW |
| --- | --- | --- | --- | --- |
| intertek | 11 | 8 | 0 | 3 |
| itv | 11 | 7 | 3 | 1 |
| marks-and-spencer | 9 | 8 | 1 | 0 |
| severn-trent | 8 | 6 | 2 | 0 |
| tesco | 11 | 7 | 2 | 2 |
| all | 50 | 36 | 8 | 6 |

## At the threshold (4 points; more is AGAINST for a non-executive)

| Director | Points | Vote | Unknowns that would settle it |
| --- | --- | --- | --- |
| Dr Gerry Murphy (tesco) | 4 | FOR | — |
| Caroline Silver (tesco) | 4 | FOR | — |
| Melissa Bethell (tesco) | 4 | FOR | — |
| Sharmila Nebhrajani OBE (severn-trent) | 4 | FOR | — |
| Christine Hodgson CBE (severn-trent) | 4 | FOR | — |
| Tamara Ingram (marks-and-spencer) | 4 | FOR | — |
| Tamara Ingram (intertek) | 4 | FOR | — |
| Graham Allan (intertek) | 4–6 | REVIEW | Bata International listed (`seat:04267576:r7:10`) |
| Graham Cooke (itv) | 2–4 | REVIEW | Qubit board_seat (`seat:04967001:r7:1`), Qubit executive (`seat:04967001:r7:1`), Qubit listed (`seat:04967001:r7:1`) |
| Andrew Cosslett (itv) | 4 | FOR | — |

## REVIEW votes

- Dame Carolyn Fairbairn (tesco): 2–3 pts; settle HSBC UK BANK PLC executive (`seat:qGYPZxoTQydJzxo2tvm5REqiQdc:09928412`)
- Thierry Garnier (tesco): 1–2 pts; settle Tesco PLC current (`seat:00445790:r7:0`)
- Kawal Preet (intertek): 1–2 pts; settle FedEx board_seat (`seat:04267576:r13:5`)
- Laura Crespi (intertek): 1–2 pts; settle YOUNGEVER HOLDINGS LTD. listed (`seat:hGs3juQ1t790y6wp6RI6BFlibvs:FC032164`)
- Graham Allan (intertek): 4–6 pts; settle Bata International listed (`seat:04267576:r7:10`)
- Graham Cooke (itv): 2–4 pts; settle Qubit board_seat (`seat:04967001:r7:1`), Qubit executive (`seat:04967001:r7:1`), Qubit listed (`seat:04967001:r7:1`)

## Votes that depend on the AIM flag

`aim_counts_as_listed` is `yes`.

| Director | unknown | yes | no |
| --- | --- | --- | --- |
| Stewart Gilliland (tesco) | REVIEW | AGAINST | FOR |

## Graded against the gold set

Policy flags: {'aim_counts_as_listed': 'yes', 'chair_designate_counts_as_chair': 'no', 'debt_only_listing_counts': 'yes'}. REVIEW is never counted wrong; it is reported as deferred.

### Roster
- recall 100.0% (50/50), precision 100.0%

### Identity (pairwise)
- precision 100.0%, recall 100.0%, F1 1.000 (3 true pairs, 3 predicted)

### Votes
- **wrong AGAINST: 0** (target 0) · wrong FOR: 0
- exact vote accuracy 89.8% on 49 scored entries; REVIEW rate 10.2%; points interval exact 83.7%
- policy: aim_counts_as_listed=yes, chair_designate_counts_as_chair=no

| gold \ predicted | FOR | AGAINST | REVIEW | MISSING |
|---|---|---|---|---|
| FOR | 36 | 0 | 5 | 0 |
| AGAINST | 0 | 8 | 0 | 0 |
| REVIEW | 0 | 0 | 0 | 0 |

**deferred (REVIEW where gold decides)**
- ITV#7 Graham Cooke (gold FOR)
- Intertek#5 Laura Crespi (gold FOR)
- Intertek#7 Graham Allan (gold FOR)
- Intertek#13 Kawal Preet (gold FOR)
- Tesco#6 Carolyn Fairbairn (gold FOR)

### Countable seats
- precision 100.0%, recall 98.0% (97 gold countable seats)
- chair unknown in the run (not wrong; gold: yes): 1
    - carolyn-fairbairn: HSBC UK Bank plc
- missed: 2
    - fiona-dawson: Kerry Group plc (not found in the run)
    - sarah-legg: Bank of Scotland plc (the run says listed no)

## Gold-graded under each AIM value

| aim_counts_as_listed | Wrong AGAINST | Wrong FOR | Accuracy | REVIEW rate |
| --- | --- | --- | --- | --- |
| unknown | 0 | 0 | 90% | 12% |
| yes | 0 | 0 | 90% | 10% |
| no | 0 | 0 | 90% | 10% |

## Hard cases (23 pass, 0 fail, 2 deferred, 1 n/a)

DEFERRED: the run left a fact unknown (a REVIEW vote, a points range, an unknown fact) instead of getting it wrong; the detail names what would settle it.

| Id | Case | Result | Detail |
| --- | --- | --- | --- |
| H1 | Ken Murphy and Gerry Murphy are two people | PASS | Ken Murphy: p:nm:00445790:r11; Gerry Murphy: p:nm:00445790:r10 |
| H2 | Titles and postnominals stripped: each anchors to CH | PASS | all on CH |
| H3 | Name structure: each anchors to CH | PASS | all on CH |
| H4 | Same person in two notices | PASS | Chris Kennedy: p:nm:00445790:r9; Sharmila Nebhrajani: p:nm:02366619:r11; Tamara Ingram: p:nm:04256886:r10 |
| H4 | 50 mentions become 47 people | PASS | 50 mentions → 47 people |
| H5 | Only the Christopher Kennedy with the anchor's date of birth links | PASS | 1 CH records, dates of birth [(1, 1964)] |
| H6 | Past roles are not current | PASS | Christine Hodgson at Standard Chartered: current=no; André Lacroix at Reckitt: current=no; Nick Hampton at Great Portland Estates: current=no; Robin Freestone at Aston Martin: current=no; Robin Freestone at Mony: current=no; Jean-Michel Valette at Boston Beer: current=no; Sarah Legg at Hang Seng: current=no |
| H7 | Advisers and committee chairs are not board seats | PASS | Gerry Murphy at Perella Weinberg: board_seat=no; Melissa Bethell at Atairos: board_seat=no; Caroline Silver at Adobe: board_seat=no; Caroline Silver at National Film and Television School: board_seat=no |
| H7 | Kawal Preet: an EVP title is not a FedEx board seat (FOR, or REVIEW) | PASS | Kawal Preet (intertek): REVIEW |
| H8 | ICE Clear Europe belongs to the Intercontinental Exchange group | PASS | ICE Clear Europe: Intercontinental Exchange, Inc |
| H8 | Hafren Dyfrdwy belongs to the Severn Trent group | PASS | Hafren Dyfrdwy: Severn Trent Plc |
| H8 | Burberry Group and The Burberry Foundation stay apart | PASS | Burberry Group: burberry-group; Burberry Foundation: burberry-foundation |
| H9 | Taken private: no equity listing | PASS | Ocean Outdoor: no; Keywords Studios: no |
| H9 | Listed though not (or not only) on CH | PASS | Magnum Ice Cream: yes; Capri Holdings: yes; Americana Restaurants: yes; Man Group: yes |
| H10 | Compound appointment lists split into one seat each | PASS | Caroline Silver at Barratt Redrow: current=yes; Caroline Silver at ICE Clear Europe: current=yes; Melissa Bethell at Diageo: current=yes; Melissa Bethell at Exor: current=yes; Melissa Bethell at Brillio: current=yes; Melissa Bethell at Magnum Ice Cream: current=yes; Karen Whitworth at Rank Group: current=yes; Karen Whitworth at Tritax Big Box: current=yes |
| H11 | M&S notice has no biographies | PASS | none: 9 |
| H12 | Exactly 4 points | DEFERRED | Gerry Murphy (tesco): 4; Christine Hodgson (severn-trent): 4; Melissa Bethell (tesco): 4; Graham Allan (intertek): 4–6 (not wrong: waiting on Bata International listed (`seat:04267576:r7:10`)); Tamara Ingram (marks-and-spencer): 4; Tamara Ingram (intertek): 4 |
| H12 | Exactly 4 points is FOR | DEFERRED | Gerry Murphy (tesco): FOR; Christine Hodgson (severn-trent): FOR; Melissa Bethell (tesco): FOR; Graham Allan (intertek): REVIEW (not wrong: waiting on Bata International listed (`seat:04267576:r7:10`)); Tamara Ingram (marks-and-spencer): FOR; Tamara Ingram (intertek): FOR |
| H12 | Caroline Silver is FOR at 4 (Moelis is an adviser role), or REVIEW | PASS | Caroline Silver (tesco): FOR |
| H13 | Stewart Gilliland follows the AIM flag | PASS | unknown: REVIEW, yes: AGAINST, no: FOR |
| H13 | Steve Mogford (chair-designate) is FOR | PASS | Steve Mogford (intertek): FOR |
| H14 | Executive rule | PASS | Nick Hampton (severn-trent): AGAINST; Helen Miles (severn-trent): AGAINST; Chris Kennedy (tesco): AGAINST; Chris Kennedy (itv): AGAINST; Dawn Allen (itv): AGAINST; Carolyn McCall (itv): AGAINST |
| H14 | Thierry Garnier's resolution was withdrawn: never AGAINST | PASS | Thierry Garnier (tesco): REVIEW |
| H15 | Named but not standing | PASS | none on the roster |
| H16 | CH outage during a nightly run | N/A | Tested, not measured on a run: `.venv/bin/pytest -k h16` |
| H17 | Lloyds group boards count once: Sarah Legg stays at 3 | PASS | Sarah Legg (severn-trent): 3 |

## Counts

- Seats by status: {'undisclosed': 30, 'confirmed': 87, 'ch_invisible': 36, 'stale': 1, 'past': 106}
- Review items by kind: {'not_in_office': 1, 'possible_same_person': 12, 'site_not_found': 3, 'stale': 1, 'undisclosed': 31, 'web_unsourced': 3}
- Organisations with cited web research: 78 of 291
- Evidence behind the votes, by source: {'notice': 134, 'ch': 97, 'web': 62}
