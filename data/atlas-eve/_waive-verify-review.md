# Waive verifier over Eve drafts

1348 sheets. Each source document is stood in by its own quotes, so every rule except substring-in-document runs as at publish time.

| Metric | Count | Share |
|---|---|---|
| Income rule (free-care line or tiers) survives Waive's verifier | 972 | 72% |
| Review-ready: income rule verified, no inconsistency, core completeness at least 50% | 926 | 68% |
| Sheets with a publish-time inconsistency | 23 | 1% |

## By generation

| Generation | Sheets | Income rule verified | Free care verified | Tiers verified | Inconsistent | Review-ready | Rejected fields |
|---|---|---|---|---|---|---|---|
| anthropic/claude-sonnet-5 | 703 | 452 | 446 | 123 | 19 | 427 | 1955 |
| opus-era | 645 | 520 | 514 | 156 | 4 | 499 | 1581 |

## By field

| Field | Accepted | Rejected | Top rejection reasons |
|---|---|---|---|
| contacts.phone | 1263 | 0 |  |
| apply.documents_required | 265 | 820 | quote does not name every listed item (685); none of the listed documents is named in the quote (134); quote not found in source (1) |
| eligibility.insured_patients_covered | 918 | 120 | quote does not say whether insured patients are covered (109); quote states the amounts-generally-billed cap, not who may apply (11) |
| eligibility.free_care_max_fpl | 960 | 77 | value not in quote (34); quote not found in source (21); quote does not state the free-care limit as an income limit (19) |
| eligibility.asset_test | 344 | 607 | quote does not support the asset-test value (607) |
| apply.submit_methods | 353 | 513 | quote does not name every listed item (480); none of the listed channels is named in the quote (31); quote not found in source (2) |
| programs.presumptive | 108 | 746 | quote does not describe presumptive (automatic) eligibility (458); quote does not name every listed item (214); none of the listed programs is named in the quote (67) |
| apply.window_days_from_first_bill | 828 | 5 | value not in quote (5) |
| contacts.languages | 729 | 2 | quote not found in source (2) |
| apply.form_url | 651 | 0 |  |
| eligibility.discount_tiers | 279 | 370 | value not in quote (362); quote not found in source (3); quote gives the discount as a range or a floor, not a fixed percentage (3) |
| apply.decision_days | 594 | 37 | value not in quote (37) |
| contacts.hours | 625 | 0 |  |
| eligibility.residency | 465 | 0 |  |
| eligibility.assistance_ceiling_fpl | 160 | 236 | quote does not state the ceiling as a percentage of the poverty level (101); quote does not say the hospital's financial assistance reaches it (84); quote states the percentage as a floor (above, over, at least), not a ceiling (29) |
| collections.eca_wait_days | 375 | 3 | value not in quote (3) |
| programs.state_programs | 314 | 0 |  |
| eligibility.min_balance | 113 | 0 |  |

## By state

| State | Sheets | Income rule verified | Review-ready |
|---|---|---|---|
| AK | 5 | 2 | 2 |
| AL | 19 | 17 | 17 |
| AR | 42 | 20 | 19 |
| AZ | 35 | 26 | 25 |
| CO | 37 | 22 | 16 |
| CT | 18 | 17 | 17 |
| DC | 2 | 2 | 2 |
| DE | 6 | 5 | 5 |
| GA | 49 | 39 | 32 |
| HI | 8 | 4 | 4 |
| IA | 42 | 34 | 33 |
| ID | 16 | 5 | 5 |
| IN | 70 | 46 | 45 |
| KS | 41 | 23 | 21 |
| KY | 58 | 42 | 41 |
| LA | 30 | 26 | 24 |
| MD | 32 | 28 | 28 |
| ME | 25 | 22 | 22 |
| MI | 88 | 78 | 73 |
| MN | 62 | 39 | 37 |
| MO | 57 | 26 | 26 |
| MS | 29 | 22 | 22 |
| MT | 34 | 18 | 18 |
| NC | 53 | 48 | 45 |
| ND | 27 | 11 | 11 |
| NE | 30 | 17 | 17 |
| NH | 21 | 14 | 13 |
| NM | 12 | 11 | 7 |
| NV | 7 | 4 | 4 |
| OK | 31 | 24 | 24 |
| OR | 36 | 30 | 30 |
| RI | 7 | 4 | 4 |
| SC | 32 | 25 | 25 |
| SD | 17 | 10 | 10 |
| TN | 38 | 21 | 19 |
| UT | 7 | 1 | 0 |
| VA | 53 | 52 | 52 |
| VT | 14 | 11 | 11 |
| WA | 22 | 21 | 19 |
| WI | 105 | 86 | 83 |
| WV | 29 | 18 | 17 |
| WY | 2 | 1 | 1 |
