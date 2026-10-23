# Refetch and verify

1340 sheets. Sources refetched with Waive's downloader and the full verifier run.

| Metric | Count |
|---|---|
| income_rule_verified | 1000 |
| all_sources_fetched | 1210 |
| no_source_fetched | 28 |
| sources_hash_match | 2565 |
| sources_hash_changed | 943 |
| accepted_fields | 10045 |
| rejected_fields | 2288 |

## Rejection reasons

| Reason | Fields |
|---|---|
| quote not found in source | 714 |
| quote does not support the asset-test value | 575 |
| value not in quote | 404 |
| source text missing | 250 |
| quote does not say whether insured patients are covered | 93 |
| quote does not state the ceiling as a percentage of the poverty level | 90 |
| quote does not say the hospital's financial assistance reaches it | 87 |
| quote states the percentage as a floor (above, over, at least), not a ceiling | 32 |
| quote does not state the free-care limit as an income limit | 18 |
| quote states the amounts-generally-billed cap, not who may apply | 11 |
| quote states the patient's share, not a discount | 4 |
| quote is about another program or a payment plan | 3 |
| quote gives the limit as the top of a later band, not the free band | 2 |
| quote states an eligibility ceiling for any assistance, not a free-care band | 2 |
| quote gives the discount as a range or a floor, not a fixed percentage | 2 |
| quote names a higher income limit than the ceiling | 1 |
