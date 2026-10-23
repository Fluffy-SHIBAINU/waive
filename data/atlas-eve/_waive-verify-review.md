# Waive verifier over Eve drafts

1340 sheets. Each source document is stood in by its own quotes, so every rule except substring-in-document runs as at publish time.

| Metric | Count | Share |
|---|---|---|
| Income rule (free-care line or tiers) survives Waive's verifier | 1059 | 79% |
| Review-ready: income rule verified, no inconsistency, core completeness at least 50% | 1003 | 74% |
| Sheets with a publish-time inconsistency | 24 | 1% |

## By generation

| Generation | Sheets | Income rule verified | Free care verified | Tiers verified | Inconsistent | Review-ready | Rejected fields |
|---|---|---|---|---|---|---|---|
| anthropic/claude-sonnet-5 | 820 | 539 | 516 | 159 | 21 | 504 | 2098 |
| opus-era | 520 | 520 | 514 | 156 | 3 | 499 | 1353 |

## By field

| Field | Accepted | Rejected | Top rejection reasons |
|---|---|---|---|
| contacts.phone | 1254 | 0 |  |
| eligibility.free_care_max_fpl | 1030 | 65 | value not in quote (21); quote not found in source (20); quote does not state the free-care limit as an income limit (20) |
| apply.documents_required | 269 | 817 | quote does not name every listed item (704); none of the listed documents is named in the quote (113) |
| eligibility.insured_patients_covered | 943 | 104 | quote does not say whether insured patients are covered (93); quote states the amounts-generally-billed cap, not who may apply (11) |
| eligibility.asset_test | 355 | 580 | quote does not support the asset-test value (580) |
| apply.submit_methods | 374 | 503 | quote does not name every listed item (474); none of the listed channels is named in the quote (28); quote not found in source (1) |
| programs.presumptive | 124 | 737 | quote does not describe presumptive (automatic) eligibility (441); quote does not name every listed item (218); none of the listed programs is named in the quote (71) |
| apply.window_days_from_first_bill | 836 | 4 | value not in quote (4) |
| contacts.languages | 737 | 0 |  |
| eligibility.discount_tiers | 315 | 356 | value not in quote (346); quote states the patient's share, not a discount (4); quote not found in source (4) |
| apply.form_url | 649 | 0 |  |
| apply.decision_days | 592 | 38 | value not in quote (38) |
| contacts.hours | 621 | 0 |  |
| eligibility.residency | 493 | 0 |  |
| eligibility.assistance_ceiling_fpl | 164 | 245 | quote does not state the ceiling as a percentage of the poverty level (98); quote does not say the hospital's financial assistance reaches it (88); quote states the percentage as a floor (above, over, at least), not a ceiling (33) |
| collections.eca_wait_days | 381 | 2 | value not in quote (2) |
| programs.state_programs | 308 | 0 |  |
| eligibility.min_balance | 116 | 0 |  |

## By state

| State | Sheets | Income rule verified | Review-ready |
|---|---|---|---|
| AK | 5 | 4 | 4 |
| AL | 19 | 17 | 17 |
| AR | 39 | 28 | 27 |
| AZ | 36 | 27 | 25 |
| CO | 39 | 25 | 19 |
| CT | 18 | 17 | 17 |
| DC | 2 | 2 | 2 |
| DE | 6 | 5 | 5 |
| GA | 48 | 44 | 37 |
| HI | 8 | 4 | 4 |
| IA | 41 | 37 | 36 |
| ID | 16 | 7 | 7 |
| IN | 67 | 49 | 48 |
| KS | 42 | 27 | 25 |
| KY | 58 | 43 | 41 |
| LA | 29 | 26 | 24 |
| MD | 32 | 28 | 28 |
| ME | 25 | 24 | 24 |
| MI | 88 | 81 | 75 |
| MN | 61 | 43 | 41 |
| MO | 56 | 33 | 32 |
| MS | 29 | 24 | 24 |
| MT | 33 | 22 | 22 |
| NC | 52 | 48 | 45 |
| ND | 24 | 13 | 13 |
| NE | 33 | 22 | 21 |
| NH | 21 | 15 | 14 |
| NM | 13 | 11 | 7 |
| NV | 7 | 6 | 5 |
| OK | 31 | 25 | 25 |
| OR | 36 | 30 | 30 |
| RI | 7 | 5 | 4 |
| SC | 29 | 25 | 25 |
| SD | 21 | 15 | 15 |
| TN | 38 | 27 | 25 |
| UT | 7 | 3 | 1 |
| VA | 52 | 52 | 52 |
| VT | 14 | 11 | 11 |
| WA | 24 | 24 | 21 |
| WI | 104 | 86 | 83 |
| WV | 28 | 23 | 21 |
| WY | 2 | 1 | 1 |
